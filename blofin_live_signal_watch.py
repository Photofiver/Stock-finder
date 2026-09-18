import json
import os
import time
import uuid

import requests

import blofin_live_approve as approval
import blofin_live_hourly as bot

SIGNAL_STATE_FILE = os.getenv("SIGNAL_STATE_FILE", "blofin_signal_state.json")
PENDING_FILE = os.getenv("PENDING_SIGNAL_FILE", "blofin_pending_signal.json")
APPROVAL_TTL_MS = 2 * 60 * 1000
CANDLE_CONFIRM_ATTEMPTS = 30
CANDLE_CONFIRM_DELAY_SEC = 1
BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "dd3511977f23cc87").strip()

_original_private_request = bot.private_request


def private_request_with_broker(method, path, params=None, body=None):
    if method.upper() != "GET" and path in {
        "/api/v1/trade/order",
        "/api/v1/trade/close-position",
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


def notify_signal(pending):
    inst = pending["inst"]
    side = pending["side"]
    rank = pending["rank"]
    message = (
        f"SYGNAL {side} {inst} | TOP{rank} | tryb AUTO. "
        "Bot od razu ponownie sprawdzi sygnal i jesli nadal jest poprawny, "
        "wykona zlecenie LIVE bez klikania."
    )
    print(message)
    send_ntfy(
        "BloFin SIGNAL - AUTO",
        message,
        priority=5,
    )


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
            side = bot.stoch_side(bars, i)
            arm = arms.get(inst, {})
            arm_dir = arm.get("direction")
            used = bool(arm.get("used", False))

            if side is None:
                detail = f"STOCH+VOL ✗ | RSI arm {arm_dir or '-'}"
            elif arm_dir != side:
                detail = f"STOCH+VOL {side} ✓ | RSI arm {arm_dir or '-'} ✗"
            elif used:
                detail = f"{side} ✓ | sygnal juz uzyty"
            else:
                detail = f"{side} ✓ | gotowy"

            lines.append(f"{rank}. {inst} — {detail}")
        except Exception as exc:
            lines.append(f"{rank}. {inst} — blad danych: {type(exc).__name__}")

    return "\n".join(lines)


def main():
    state = load_json(SIGNAL_STATE_FILE, default_signal_state())
    top10, _tickers, _instruments = bot.get_universe()
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

    wait_for_confirmed_hourly_close(top10, expected_close_ms)

    candidates = bot.evaluate_signals(state, top10)
    created = None
    if candidates:
        rank, inst, side, signal_close_ms = candidates[0]
        if int(signal_close_ms) != expected_close_ms:
            raise RuntimeError(
                f"Candidate candle mismatch: got {signal_close_ms}, "
                f"expected {expected_close_ms}"
            )
        now = bot.now_ms()
        created = {
            "version": 2,
            "status": "pending",
            "signal_id": uuid.uuid4().hex,
            "inst": inst,
            "side": side,
            "rank": int(rank),
            "signal_close_ms": int(signal_close_ms),
            "created_at_ms": now,
            "expires_at_ms": now + APPROVAL_TTL_MS,
        }
        save_json(PENDING_FILE, created)

        notify_signal(created)
        approval.main()
    else:
        diagnostic = build_live_diagnostic(state, top10)
        print(diagnostic)
        send_ntfy("BloFin LIVE check", diagnostic, priority=2)

    state["last_scan_close_ms"] = expected_close_ms
    state["last_run_ms"] = bot.now_ms()
    state["last_top10"] = top10
    save_json(SIGNAL_STATE_FILE, state)
    print(
        json.dumps(
            {
                "pending": created,
                "hour_close_ms": expected_close_ms,
                "top10": top10,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
