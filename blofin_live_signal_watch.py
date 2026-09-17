import hashlib
import hmac
import json
import os
import uuid
from urllib.parse import urlencode

import requests

import blofin_live_hourly as bot

SIGNAL_STATE_FILE = os.getenv("SIGNAL_STATE_FILE", "blofin_signal_state.json")
PENDING_FILE = os.getenv("PENDING_SIGNAL_FILE", "blofin_pending_signal.json")
APPROVAL_TTL_MS = 2 * 60 * 1000
APPROVAL_URL = "https://github.com/Photofiver/Stock-finder/actions/workflows/blofin_live_manual.yml"
ONE_CLICK_RELAY_URL = os.getenv("ONE_CLICK_RELAY_URL", "").strip()
ONE_CLICK_HMAC_SECRET = os.getenv("ONE_CLICK_HMAC_SECRET", "").strip()


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


def signature_payload(pending):
    return "|".join(
        [
            str(pending["signal_id"]),
            str(pending["inst"]),
            str(pending["side"]),
            str(pending["signal_close_ms"]),
            str(pending["created_at_ms"]),
            str(pending["expires_at_ms"]),
        ]
    )


def approval_signature(pending):
    if not ONE_CLICK_HMAC_SECRET:
        return ""
    return hmac.new(
        ONE_CLICK_HMAC_SECRET.encode("utf-8"),
        signature_payload(pending).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def notify_signal(pending):
    inst = pending["inst"]
    side = pending["side"]
    rank = pending["rank"]
    message = (
        f"SYGNAL {side} {inst} | TOP{rank} | masz 2 minuty na zatwierdzenie. "
        "Po 2 minutach sygnal wygasa i zlecenie nie zostanie wyslane."
    )
    print(message)

    signature = approval_signature(pending)
    one_click_ready = bool(ONE_CLICK_RELAY_URL and signature)

    payload = {
        "topic": bot.NTFY_TOPIC,
        "message": message,
        "title": "BloFin SIGNAL - 2 min",
        "priority": 5,
        "click": APPROVAL_URL,
    }

    if one_click_ready:
        params = {
            "signal_id": pending["signal_id"],
            "inst": pending["inst"],
            "side": pending["side"],
            "signal_close_ms": str(pending["signal_close_ms"]),
            "created_at_ms": str(pending["created_at_ms"]),
            "expires_at_ms": str(pending["expires_at_ms"]),
            "signature": signature,
        }
        separator = "&" if "?" in ONE_CLICK_RELAY_URL else "?"
        approve_url = ONE_CLICK_RELAY_URL + separator + urlencode(params)
        payload["actions"] = [
            {
                "action": "http",
                "label": "ZATWIERDZ",
                "url": approve_url,
                "method": "POST",
                "clear": True,
            }
        ]
    else:
        payload["actions"] = [
            {
                "action": "view",
                "label": "ZATWIERDZ",
                "url": APPROVAL_URL,
                "clear": True,
            }
        ]
        print("ONE-CLICK NOT READY: secure relay is not configured")

    try:
        requests.post(
            "https://ntfy.sh/",
            json=payload,
            timeout=bot.HTTP_TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        print(f"NTFY ERROR: {exc}")


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

    state["last_run_ms"] = bot.now_ms()
    state["last_top10"] = top10
    save_json(SIGNAL_STATE_FILE, state)
    print(json.dumps({"pending": created, "top3": top10[:3]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
