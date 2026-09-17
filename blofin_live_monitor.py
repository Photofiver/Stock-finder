import json

import blofin_live_hourly as bot


def main():
    bot.require_live_enabled()
    state = bot.load_state()
    pos = state.get("position")
    if not pos:
        print(json.dumps({"position": None, "changed": False}))
        return

    open_positions = bot.get_open_positions()
    matching = [p for p in open_positions if p.get("instId") == pos.get("inst")]
    if not matching:
        bot.record_closed(state, "TP/SL or external close")
        bot.save_state(state)
        print(json.dumps({"position": None, "changed": True}))
        return

    age_ms = bot.now_ms() - int(pos.get("opened_ms") or bot.now_ms())
    if age_ms >= bot.HOLD_HOURS * bot.D1H_MS and not pos.get("max_hold_alerted"):
        pos["max_hold_alerted"] = True
        bot.notify(
            f"{pos['side']} {pos['inst']} jest otwarta juz {bot.HOLD_HOURS}h. "
            "Limit czasu zostal osiagniety; pozycja nie zostala automatycznie zamknieta.",
            "BloFin LIVE 5H",
        )
        bot.save_state(state)
        print(json.dumps({"position": pos, "changed": True}, ensure_ascii=False))
        return

    print(json.dumps({"position": pos, "changed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
