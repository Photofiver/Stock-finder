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

    changed = bot.cancel_tracked_tpsl(state)
    if changed:
        bot.save_state(state)

    print(json.dumps({"position": pos, "changed": changed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
