import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import blofin_live_hourly as bot

SIGNAL_STATE_FILE = os.getenv("SIGNAL_STATE_FILE", "blofin_signal_state.json")
CANDLE_CONFIRM_ATTEMPTS = 30
CANDLE_CONFIRM_DELAY_SEC = 1
BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "dd3511977f23cc87").strip()
UK_TZ = ZoneInfo("Europe/London")

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
    headers = {
        "Title": title,
        "Priority": str(priority),
    }
    if actions:
        headers["Actions"] = actions
    if click:
        headers["Click"] = click
    try:
        requests.post(
            f"https://ntfy.sh/{bot.NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=bot.HTTP_TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        print(f"NTFY ERROR: {exc}")


def expected_signal_close_ms():
    now = bot.now_ms()
    return (now // bot.SIGNAL_MS) * bot.SIGNAL_MS


def wait_for_confirmed_signal_close(top7, expected_close_ms):
    pending = set(top7)
    for attempt in range(CANDLE_CONFIRM_ATTEMPTS):
        ready = []
        for inst in list(pending):
            try:
                bars = bot.fetch_signal_bars(inst)
                if bars:
                    latest_close_ms = bot.bar_close_ms(bars[-1])
                    if latest_close_ms >= expected_close_ms:
                        ready.append(inst)
            except Exception as exc:
                print(f"CANDLE CONFIRM {inst}: {type(exc).__name__}: {exc}")
        for inst in ready:
            pending.discard(inst)
        if not pending:
            print(
                f"{bot.SIGNAL_LABEL} candle {expected_close_ms} confirmed for all TOP{bot.TOP_N} "
                f"after {attempt + 1} check(s)."
            )
            return
        if attempt < CANDLE_CONFIRM_ATTEMPTS - 1:
            time.sleep(CANDLE_CONFIRM_DELAY_SEC)

    raise RuntimeError(
        f"{bot.SIGNAL_LABEL} candle not confirmed in time for: " + ", ".join(sorted(pending))
    )


def build_live_diagnostic(state, top7):
    lines = [
        f"{bot.SIGNAL_LABEL} TOP{bot.TOP_N} — BRAK WEJSCIA",
        "SHORT 4/4: MACD↓ + VOL↓ + VOL<MA5/10 + spike",
        "LONG 4/4: MACD↑ + VOL↑ + VOL>MA5/10 + trough",
        "",
    ]

    for rank, inst in enumerate(top7, start=1):
        try:
            bars = bot.fetch_signal_bars(inst)
            if len(bars) < 40:
                lines.append(f"{rank}. {inst} — brak danych")
                continue

            i = len(bars) - 1
            s = bot.short_entry_metrics(bars, i)
            l = bot.long_entry_metrics(bars, i)
            if not s or not l:
                lines.append(f"{rank}. {inst} — brak danych MACD/Volume")
                continue

            s_checks = [
                ("MACD↓", s["macd_cross_down"]),
                ("VOL↓", s["volume_declining"]),
                ("VOL<MA", s["volume_below_mas"]),
                ("spike", s["recent_spike"]),
            ]
            l_checks = [
                ("MACD↑", l["macd_cross_up"]),
                ("VOL↑", l["volume_rising"]),
                ("VOL>MA", l["volume_above_mas"]),
                ("trough", l["recent_trough"]),
            ]

            s_ok = sum(1 for _, ok in s_checks if ok)
            l_ok = sum(1 for _, ok in l_checks if ok)
            s_missing = ", ".join(name for name, ok in s_checks if not ok)
            l_missing = ", ".join(name for name, ok in l_checks if not ok)

            lines.append(f"{rank}. {inst}")
            lines.append(
                f"   S {s_ok}/4" + ("" if s_ok == 4 else f" | brak: {s_missing}")
            )
            lines.append(
                f"   L {l_ok}/4" + ("" if l_ok == 4 else f" | brak: {l_missing}")
            )
        except Exception as exc:
            lines.append(f"{rank}. {inst} — blad danych: {type(exc).__name__}")

    return "\n".join(lines)


def main():
    bot.require_live_enabled()
    bot.require_account_modes()

    state = bot.load_state()
    top7, tickers, instruments = bot.get_universe()
    if not top7:
        raise RuntimeError("BloFin TOP7 is empty")

    expected_close_ms = expected_signal_close_ms()
    last_scan_close_ms = int(state.get("last_scan_close_ms") or 0)
    if last_scan_close_ms == expected_close_ms:
        print(
            json.dumps(
                {
                    "duplicate_scan_skipped": True,
                    "signal_close_ms": expected_close_ms,
                    "top7": top7,
                },
                ensure_ascii=False,
            )
        )
        return

    # Start just after the signal boundary and confirm the closed signal candle for TOP7
    # plus the tracked instrument, because exits are based on its Volume flip.
    tracked_insts = list(bot.get_tracked_positions(state))
    confirm_insts = list(dict.fromkeys(top7 + tracked_insts))
    wait_for_confirmed_signal_close(confirm_insts, expected_close_ms)

    bot.sync_all_tracked_positions(state)
    bot.ensure_tp1_for_all_tracked_positions(state)
    candidates = bot.evaluate_signals(state, top7)

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
        diagnostic = build_live_diagnostic(state, top7)
        print(diagnostic)
        send_ntfy("BloFin LIVE check", diagnostic, priority=2)
    else:
        send_ntfy(
            "BloFin LIVE check",
            f"Skan {bot.SIGNAL_LABEL} wykonany. Kandydaci: {len(candidates)}, wykonane: {len(executed)}.",
            priority=2,
        )

    state["last_scan_close_ms"] = expected_close_ms
    state["last_run_ms"] = bot.now_ms()
    state["last_top7"] = top7
    bot.save_state(state)

    print(
        json.dumps(
            {
                "executed": executed,
                "signal_close_ms": expected_close_ms,
                "top7": top7,
                "positions": bot.get_tracked_positions(state),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
