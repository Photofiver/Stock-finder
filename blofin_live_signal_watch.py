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


def expected_hour_close_ms():
    return (bot.now_ms() // bot.D1H_MS) * bot.D1H_MS


def wait_for_confirmed_hourly_close(top10, expected_close_ms):
    pending = set(top10)
    for attempt in range(CANDLE_CONFIRM_ATTEMPTS):
        ready = []
        for inst in list(pending):
            try:
                bars = bot.fetch_1h(inst)
                if bars:
                    latest_close_ms = int(bars[-1]["ts"] + bot.D1H_MS)
                    if latest_close_ms >= expected_close_ms:
                        ready.append(inst)
            except Exception as exc:
                print(f"CANDLE CONFIRM {inst}: {type(exc).__name__}: {exc}")
        for inst in ready:
            pending.discard(inst)
        if not pending:
            print(
                f"Hourly candle {expected_close_ms} confirmed for all TOP10 "
                f"after {attempt + 1} check(s)."
            )
            return
        if attempt < CANDLE_CONFIRM_ATTEMPTS - 1:
            time.sleep(CANDLE_CONFIRM_DELAY_SEC)

    raise RuntimeError(
        "Hourly candle not confirmed in time for: " + ", ".join(sorted(pending))
    )


def build_live_diagnostic(state, top10):
    lines = ["Brak sygnalu LIVE. Sprawdzono aktualne TOP10 BloFin 24h:"]
    arms = state.get("arms", {})

    for rank, inst in enumerate(top10, start=1):
        try:
            bars = bot.fetch_1h(inst)
            if len(bars) < 35:
                lines.append(f"{rank}. {inst} — za malo danych 1H")
                continue

            i = len(bars) - 1
            arm = arms.get(inst, {})
            arm_dir = arm.get("direction")
            vol_ok = (
                arm_dir in ("LONG", "SHORT")
                and bot.volume_ok(bars, i, arm_dir)
            )

            if arm_dir not in ("LONG", "SHORT"):
                detail = "RSI arm ✗ | VOL ✗"
            elif vol_ok:
                detail = f"RSI arm {arm_dir} ✓ | VOL {arm_dir} ✓ | gotowy"
            else:
                detail = f"RSI arm {arm_dir} ✓ | VOL {arm_dir} ✗"

            lines.append(f"{rank}. {inst} — {detail}")
        except Exception as exc:
            lines.append(f"{rank}. {inst} — blad danych: {type(exc).__name__}")

    return "\n".join(lines)


def main():
    bot.require_live_enabled()
    bot.require_account_modes()

    state = bot.load_state()
    top10, tickers, instruments = bot.get_universe()
    if not top10:
        raise RuntimeError("BloFin TOP10 is empty")

    expected_close_ms = expected_hour_close_ms()
    last_scan_close_ms = int(state.get("last_scan_close_ms") or 0)
    if last_scan_close_ms == expected_close_ms:
        print(
            json.dumps(
                {
                    "duplicate_scan_skipped": True,
                    "hour_close_ms": expected_close_ms,
                    "top10": top10,
                },
                ensure_ascii=False,
            )
        )
        return

    # Start just after xx:00 and proceed as soon as BloFin confirms the closed 1H candle.
    wait_for_confirmed_hourly_close(top10, expected_close_ms)

    open_positions = bot.sync_tracked_position(state)
    candidates = bot.evaluate_signals(state, top10)
    executed = None

    if not state.get("position"):
        if open_positions:
            names = ", ".join(str(p.get("instId")) for p in open_positions[:5])
            bot.notify(
                f"No new LIVE order: account already has an open position ({names}).",
                "BloFin LIVE BLOCKED",
            )
        elif candidates:
            candidate = candidates[0]
            rank, inst, side, signal_close_ms = candidate
            if int(signal_close_ms) != expected_close_ms:
                raise RuntimeError(
                    f"Candidate candle mismatch: got {signal_close_ms}, "
                    f"expected {expected_close_ms}"
                )

            # No pending approval and no second signal scan:
            # the validated signal is sent to market immediately.
            print(
                f"DIRECT LIVE {side} {inst} | TOP{rank} | "
                f"signal age {(bot.now_ms() - signal_close_ms) / 1000:.1f}s"
            )
            bot.place_live_trade(state, candidate, tickers, instruments)
            executed = {
                "inst": inst,
                "side": side,
                "rank": int(rank),
                "signal_close_ms": int(signal_close_ms),
            }
    elif candidates:
        print("Signal found, but an existing tracked LIVE position blocks a new entry.")

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
                "hour_close_ms": expected_close_ms,
                "top10": top10,
                "position": state.get("position"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
