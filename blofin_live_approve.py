import json
import os

import blofin_live_hourly as bot

PENDING_FILE = os.getenv("PENDING_SIGNAL_FILE", "blofin_pending_signal.json")
APPROVAL_TTL_MS = 2 * 60 * 1000


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


def reject(pending, status, message):
    if pending:
        pending["status"] = status
        pending["resolved_at_ms"] = bot.now_ms()
        save_pending(pending)
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

    bot.require_account_modes()
    state = bot.load_state()
    open_positions = bot.get_open_positions()
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
    current_side = bot.stoch_side(bars, i)
    arm_direction = current_arm_direction(bars, i)

    if current_close_ms != signal_close_ms or current_side != side or arm_direction != side:
        reject(pending, "stale", "Warunki sygnalu zmienily sie przed zatwierdzeniem. Nie wyslano zlecenia.")
        return

    rank = top10.index(inst) + 1
    state.setdefault("arms", {})[inst] = {
        "direction": side,
        "used": False,
        "last_cross_close_ms": signal_close_ms,
    }
    candidate = (rank, inst, side, signal_close_ms)
    bot.place_live_trade(state, candidate, tickers, instruments)

    pending["status"] = "consumed"
    pending["approved_at_ms"] = bot.now_ms()
    save_pending(pending)
    state["last_run_ms"] = bot.now_ms()
    state["last_top10"] = top10
    bot.save_state(state)

    print(json.dumps({
        "approved": True,
        "inst": inst,
        "side": side,
        "rank": rank,
        "position": state.get("position"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
