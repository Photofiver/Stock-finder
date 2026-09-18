import hashlib
import hmac
import json
import os

import blofin_live_hourly as bot

PENDING_FILE = os.getenv("PENDING_SIGNAL_FILE", "blofin_pending_signal.json")
APPROVAL_TTL_MS = 2 * 60 * 1000
ONE_CLICK_HMAC_SECRET = os.getenv("ONE_CLICK_HMAC_SECRET", "").strip()
BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "dd3511977f23cc87").strip()

_original_private_request = bot.private_request


def private_request_with_broker(method, path, params=None, body=None):
    if method.upper() == "POST" and path in {
        "/api/v1/trade/order",
        "/api/v1/trade/close-position",
        "/api/v1/trade/order-tpsl",
        "/api/v1/trade/order-algo",
    }:
        body = dict(body or {})
        if BROKER_ID:
            body.setdefault("brokerId", BROKER_ID)
    return _original_private_request(method, path, params=params, body=body)


bot.private_request = private_request_with_broker


def load_pending():
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def save_pending(data):
    tmp = PENDING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, PENDING_FILE)


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


def pending_from_inputs():
    signal_id = os.getenv("APPROVAL_SIGNAL_ID", "").strip()
    if not signal_id:
        return None
    if not ONE_CLICK_HMAC_SECRET:
        raise RuntimeError("Missing ONE_CLICK_HMAC_SECRET")

    pending = {
        "version": 2,
        "status": "pending",
        "signal_id": signal_id,
        "inst": os.getenv("APPROVAL_INST", "").strip(),
        "side": os.getenv("APPROVAL_SIDE", "").strip(),
        "signal_close_ms": int(os.getenv("APPROVAL_SIGNAL_CLOSE_MS", "0") or 0),
        "created_at_ms": int(os.getenv("APPROVAL_CREATED_AT_MS", "0") or 0),
        "expires_at_ms": int(os.getenv("APPROVAL_EXPIRES_AT_MS", "0") or 0),
    }
    supplied = os.getenv("APPROVAL_SIGNATURE", "").strip()
    expected = hmac.new(
        ONE_CLICK_HMAC_SECRET.encode("utf-8"),
        signature_payload(pending).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise RuntimeError("Invalid LIVE approval signature")
    return pending


def persist_resolution(pending, status):
    stored = load_pending()
    if stored and stored.get("signal_id") == pending.get("signal_id"):
        stored["status"] = status
        stored["resolved_at_ms"] = bot.now_ms()
        save_pending(stored)


def reject(pending, status, message):
    if pending:
        persist_resolution(pending, status)
    bot.notify(message, "BloFin LIVE NO ORDER")
    print(message)


def current_arm_direction(bars, latest_index):
    direction = None
    for i in range(1, latest_index + 1):
        cross = bot.rsi_cross(bars, i)
        if cross:
            direction = cross
    return direction


def main():
    bot.require_live_enabled()

    pending = pending_from_inputs()
    if pending is None:
        pending = load_pending()

    if not pending or pending.get("status") != "pending":
        reject(pending, "missing", "Brak aktywnego sygnalu do zatwierdzenia.")
        return

    now = bot.now_ms()
    created_at = int(pending.get("created_at_ms") or 0)
    expires_at = int(pending.get("expires_at_ms") or (created_at + APPROVAL_TTL_MS))
    if created_at <= 0 or now > expires_at or now - created_at > APPROVAL_TTL_MS:
        reject(pending, "expired", "Sygnal wygasl po 2 minutach. Nie wyslano zlecenia.")
        return

    signal_close_ms = int(pending.get("signal_close_ms") or 0)
    signal_age_ms = now - signal_close_ms
    if (
        signal_close_ms <= 0
        or signal_age_ms < 0
        or signal_age_ms > bot.SIGNAL_MAX_AGE_MS
    ):
        reject(
            pending,
            "stale",
            f"Sygnal jest za stary ({signal_age_ms / 1000:.0f}s od zamkniecia swiecy; "
            f"limit {bot.SIGNAL_MAX_AGE_MS / 1000:.0f}s). Nie wyslano zlecenia.",
        )
        return

    bot.require_account_modes()
    state = bot.load_state()
    tracked_before = state.get("position")
    open_positions = bot.sync_tracked_position(state)
    if tracked_before and not state.get("position"):
        bot.save_state(state)

    if state.get("position"):
        reject(pending, "blocked", "Masz juz sledzona pozycje LIVE. Nowe zlecenie nie zostalo wyslane.")
        return
    if open_positions:
        names = ", ".join(str(p.get("instId")) for p in open_positions[:5])
        reject(pending, "blocked", f"Na koncie jest juz otwarta pozycja ({names}). Nowe zlecenie nie zostalo wyslane.")
        return

    top10, tickers, instruments = bot.get_universe()
    inst = str(pending.get("inst") or "")
    side = str(pending.get("side") or "")
    signal_close_ms = int(pending.get("signal_close_ms") or 0)

    if inst not in top10 or inst not in tickers or inst not in instruments:
        reject(pending, "stale", "Sygnal nie jest juz w aktualnym TOP10. Nie wyslano zlecenia.")
        return

    bars = bot.fetch_1h(inst)
    if len(bars) < 35:
        reject(pending, "stale", "Za malo danych do ponownego sprawdzenia sygnalu. Nie wyslano zlecenia.")
        return

    i = len(bars) - 1
    current_close_ms = int(bars[i]["ts"] + bot.D1H_MS)
    arm_direction = current_arm_direction(bars, i)
    volume_matches = (
        arm_direction in ("LONG", "SHORT")
        and bot.volume_ok(bars, i, arm_direction)
    )

    if (
        current_close_ms != signal_close_ms
        or arm_direction != side
        or not volume_matches
    ):
        reject(pending, "stale", "Warunki RSI/Volume zmienily sie przed zatwierdzeniem. Nie wyslano zlecenia.")
        return

    rank = top10.index(inst) + 1
    state.setdefault("arms", {})[inst] = {
        "direction": side,
        "used": False,
        "last_cross_close_ms": signal_close_ms,
    }
    candidate = (rank, inst, side, signal_close_ms)
    bot.place_live_trade(state, candidate, tickers, instruments)

    stored = load_pending()
    if stored and stored.get("signal_id") == pending.get("signal_id"):
        stored["status"] = "consumed"
        stored["approved_at_ms"] = bot.now_ms()
        save_pending(stored)

    state["last_run_ms"] = bot.now_ms()
    state["last_top10"] = top10
    bot.save_state(state)

    print(json.dumps({
        "approved": True,
        "signal_id": pending.get("signal_id"),
        "inst": inst,
        "side": side,
        "rank": rank,
        "position": state.get("position"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
