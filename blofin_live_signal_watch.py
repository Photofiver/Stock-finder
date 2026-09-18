import json
import os
import time
import requests

import blofin_live_hourly as bot

SIGNAL_STATE_FILE = os.getenv("SIGNAL_STATE_FILE", "blofin_signal_state.json")
CANDLE_CONFIRM_ATTEMPTS = 30
CANDLE_CONFIRM_DELAY_SEC = 1
BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "dd3511977f23cc87").strip()

_original_private_request = bot.private_request


def private_request_with_broker(method, path, params=None, body=None):
    if method.upper() != "GET" and path in {
        "/api/v1/trade/order",
        "/api/v1/trade/close-position",
        "/api/v1/trade/order-tpsl",
        "/api/v1/trade/order-algo",
    }:
        body = dict(body or {})
        body.setdefault("brokerId", BROKER_ID)
    return _original_private_request(method, path, params=params, body=body)


bot.private_request = private_request_with_broker


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def default_signal_state():
    return {
        "version": 1,
        "started_at_ms": bot.now_ms(),
        "position": None,
        "arms": {},
        "last_processed_close_ms": {},
        "trades": {"total": 0, "wins": 0, "losses": 0, "flat": 0},
        "realized_pnl_usdt": 0.0,
    }


def send_ntfy(title, message, priority=2, actions=None, click=None):
    payload = {
        "topic": bot.NTFY_TOPIC,
        "message": message,
        "title": title,
        "priority": priority,
    }
    if actions:
        payload["actions"] = actions
    if click:
        payload["click"] = click
    try:
        requests.post(
            "https://ntfy.sh/",
            json=payload,
            timeout=bot.HTTP_TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        print(f"NTFY ERROR: {exc}")


def expected_signal_close_ms():
    return (bot.now_ms() // bot.SIGNAL_MS) * bot.SIGNAL_MS


def wait_for_confirmed_signal_close(top10, expected_close_ms):
    pending = set(top10)
    for attempt in range(CANDLE_CONFIRM_ATTEMPTS):
        ready = []
        for inst in list(pending):
            try:
                bars = bot.fetch_signal_bars(inst)
                if bars:
                    latest_close_ms = int(bars[-1]["ts"] + bot.SIGNAL_MS)
                    if latest_close_ms >= expected_close_ms:
                        ready.append(inst)
            except Exception as exc:
                print(f"CANDLE CONFIRM {inst}: {type(exc).__name__}: {exc}")
        for inst in ready:
            pending.discard(inst)
        if not pending:
            print(
                f"{bot.SIGNAL_MINUTES}m candle {expected_close_ms} confirmed for all TOP10 "
                f"after {attempt + 1} check(s)."
            )
            return
        if attempt < CANDLE_CONFIRM_ATTEMPTS - 1:
            time.sleep(CANDLE_CONFIRM_DELAY_SEC)

    raise RuntimeError(
        f"{bot.SIGNAL_MINUTES}m candle not confirmed in time for: " + ", ".join(sorted(pending))
    )


def build_live_diagnostic(state, top10):
    lines = ["Brak sygnalu LIVE. Sprawdzono aktualne TOP10 BloFin 24h:"]

    for rank, inst in enumerate(top10, start=1):
        try:
            bars = bot.fetch_signal_bars(inst)
            if len(bars) < 2:
                lines.append(f"{rank}. {inst} — za malo danych {bot.SIGNAL_MINUTES}m")
                continue

            i = len(bars) - 1
            signal = bot.volume_flip_signal(bars, i)
            prev_color = bot.candle_color(bars[i - 1])
            cur_color = bot.candle_color(bars[i])
            if signal:
                detail = (
                    f"VOL {prev_color}->{cur_color} ✓ | "
                    f"{bars[i]['v']:.4f}>{bars[i - 1]['v']:.4f} | {signal}"
                )
            else:
                detail = (
                    f"VOL {prev_color}->{cur_color} ✗ | "
                    f"{bars[i]['v']:.4f} vs {bars[i - 1]['v']:.4f}"
                )
            lines.append(f"{rank}. {inst} — {detail}")
        except Exception as exc:
            lines.append(f"{rank}. {inst} — blad danych: {type(exc).__name__}")

    return "\n".join(lines)


def main():
    # Stop any already-running legacy hourly loop after it refreshes the repo.
    # The new workflow is a one-shot scheduled scan and is not affected.
    if os.getenv("GITHUB_WORKFLOW") == "BloFin LIVE 10m loop":
        raise RuntimeError("Legacy persistent 10m loop disabled; use one-shot scheduled scans.")

    if (
        bot.SIGNAL_MINUTES == 10
        and os.getenv("GITHUB_WORKFLOW") == "BloFin LIVE signal watch"
    ):
        print("10m LIVE is handled by the dedicated loop workflow; duplicate run skipped.")
        return

    bot.require_live_enabled()
    bot.require_account_modes()

    state = bot.load_state()
    top10, tickers, instruments = bot.get_universe()
    if not top10:
        raise RuntimeError("BloFin TOP10 is empty")

    expected_close_ms = expected_signal_close_ms()
    last_scan_close_ms = int(state.get("last_scan_close_ms") or 0)
    if last_scan_close_ms == expected_close_ms:
        print(
            json.dumps(
                {
                    "duplicate_scan_skipped": True,
                    "signal_close_ms": expected_close_ms,
                    "top10": top10,
                },
                ensure_ascii=False,
            )
        )
        return

    # Start just after the signal boundary and confirm the closed signal candle for TOP10
    # plus the tracked instrument, because exits are based on its Volume flip.
    tracked_insts = list(bot.get_tracked_positions(state))
    confirm_insts = list(dict.fromkeys(top10 + tracked_insts))
    wait_for_confirmed_signal_close(confirm_insts, expected_close_ms)

    bot.sync_all_tracked_positions(state)
    bot.evaluate_all_tracked_exit_signals(state, expected_close_ms)
    candidates = bot.evaluate_signals(state, top10)

    for rank, inst, side, signal_close_ms in candidates:
        if int(signal_close_ms) != expected_close_ms:
            raise RuntimeError(
                f"Candidate candle mismatch: got {signal_close_ms}, "
                f"expected {expected_close_ms}"
            )
        print(
            f"DIRECT LIVE candidate {side} {inst} | TOP{rank} | "
            f"signal age {(bot.now_ms() - signal_close_ms) / 1000:.1f}s"
        )

    executed = bot.execute_candidate_batch(state, candidates, tickers, instruments)

    if not candidates:
        diagnostic = build_live_diagnostic(state, top10)
        print(diagnostic)
        send_ntfy("BloFin LIVE check", diagnostic, priority=2)

    state["last_scan_close_ms"] = expected_close_ms
    state["last_run_ms"] = bot.now_ms()
    state["last_top10"] = top10
    bot.save_state(state)

    print(
        json.dumps(
            {
                "executed": executed,
                "signal_close_ms": expected_close_ms,
                "top10": top10,
                "positions": bot.get_tracked_positions(state),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
