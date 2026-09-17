import json
import os
import uuid

import requests

import blofin_live_hourly as bot

SIGNAL_STATE_FILE = os.getenv("SIGNAL_STATE_FILE", "blofin_signal_state.json")
PENDING_FILE = os.getenv("PENDING_SIGNAL_FILE", "blofin_pending_signal.json")
APPROVAL_TTL_MS = 2 * 60 * 1000
APPROVAL_URL = "https://github.com/Photofiver/Stock-finder/actions/workflows/blofin_live_manual.yml"


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
        f"SYGNAL {side} {inst} | TOP{rank} | masz 2 minuty na zatwierdzenie. "
        "Kliknij ZATWIERDZ, a potem Run workflow w GitHub. "
        "Po 2 minutach sygnal wygasa i zlecenie nie zostanie wyslane."
    )
    print(message)
    send_ntfy(
        "BloFin SIGNAL - 2 min",
        message,
        priority=5,
        click=APPROVAL_URL,
        actions=[
            {
                "action": "view",
                "label": "ZATWIERDZ",
                "url": APPROVAL_URL,
                "clear": True,
            }
        ],
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

    candidates = bot.evaluate_signals(state, top10)
    created = None
    if candidates:
        rank, inst, side, signal_close_ms = candidates[0]
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
    else:
        diagnostic = build_live_diagnostic(state, top10)
        print(diagnostic)
        send_ntfy("BloFin LIVE check", diagnostic, priority=2)

    state["last_run_ms"] = bot.now_ms()
    state["last_top10"] = top10
    save_json(SIGNAL_STATE_FILE, state)
    print(json.dumps({"pending": created, "top10": top10}, ensure_ascii=False))


if __name__ == "__main__":
    main()
