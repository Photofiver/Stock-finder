# Volume-flip LIVE multi-position monitor
import json

import blofin_live_hourly as bot


def main():
    bot.require_live_enabled()
    state = bot.load_state()
    before = set(bot.get_tracked_positions(state))
    if not before:
        print(json.dumps({"positions": {}, "changed": False}))
        return

    bot.sync_all_tracked_positions(state)
    after = set(bot.get_tracked_positions(state))
    changed = before != after

    bot.save_state(state)
    print(json.dumps({
        "positions": bot.get_tracked_positions(state),
        "changed": changed,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
