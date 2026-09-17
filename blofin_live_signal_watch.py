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


def notify_signal(pending, approval_topic):
    inst = pending["inst"]
    side = pending["side"]
    rank = pending["rank"]
    message = (
        f"SYGNAL {side} {inst} | TOP{rank} | masz 2 minuty. "
        "Kliknij tylko ZATWIERDZ. Po kliknieciu bot od razu ponownie sprawdzi sygnal "
        "i jesli nadal jest poprawny, wykona zlecenie LIVE."
    )
    print(message)
    send_ntfy(
        "BloFin SIGNAL - 2 min",
        message,
        priority=5,
        actions=[
            {
                "action": "http",
                "label": "ZATWIERDZ",
                "url": f"https://ntfy.sh/{approval_topic}",
                "method": "POST",
                "body": pending["signal_id"],
                "clear": True,
            }
        ],
    )


def wait_for_approval(approval_topic, signal_id, expires_at_ms):
    url = f"https://ntfy.sh/{approval_topic}/json"
    print("Czekam maksymalnie 2 minuty na jedno klikniecie ZATWIERDZ...")

    while bot.now_ms() <= expires_at_ms:
        remaining_s = max(1.0, (expires_at_ms - bot.now_ms()) / 1000.0)
        read_timeout = min(15.0, max(2.0, remaining_s))
        try:
            with requests.get(
                url,
                params={"since": "all"},
                stream=True,
                timeout=(5, read_timeout),
            ) as response:
                response.raise_for_status()
                for raw in response.iter_lines():
                    if bot.now_ms() > expires_at_ms:
                        return False
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except Exception:
                        continue
                    if event.get("event") != "message":
                        continue
                    if str(event.get("message") or "").strip() == signal_id:
                        print("Odebrano ZATWIERDZ z ntfy.")
                        return True
        except Exception as exc:
            if bot.now_ms() > expires_at_ms:
                break
            print(f"Approval listener retry: {type(exc).__name__}: {exc}")
            time.sleep(min(1.0, max(0.0, (expires_at_ms - bot.now_ms()) / 1000.0)))

    return False


def expire_pending(pending):
    stored = load_json(PENDING_FILE, {})
    if stored.get("signal_id") != pending.get("signal_id"):
        return
    if stored.get("status") != "pending":
        return
    stored["status"] = "expired"
    stored["resolved_at_ms"] = bot.now_ms()
    save_json(PENDING_FILE, stored)
    print("Minely 2 minuty bez zatwierdzenia. Zlecenia nie wyslano.")


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

        approval_topic = f"blofin-approve-{uuid.uuid4().hex}"
        notify_signal(created, approval_topic)

        if wait_for_approval(
            approval_topic,
            created["signal_id"],
            created["expires_at_ms"],
        ):
            approval.main()
        else:
            expire_pending(created)
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
