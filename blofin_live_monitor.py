# Volume-flip LIVE multi-position monitor
import json
import os

import blofin_live_hourly as bot

BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "dd3511977f23cc87").strip()
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


def main():
    bot.require_live_enabled()
    state = bot.load_state()
    before = set(bot.get_tracked_positions(state))
    if not before:
        print(json.dumps({"positions": {}, "changed": False}))
        return

    bot.sync_all_tracked_positions(state)
    bot.ensure_tp1_for_all_tracked_positions(state)
    after = set(bot.get_tracked_positions(state))
    changed = before != after

    bot.save_state(state)
    print(json.dumps({
        "positions": bot.get_tracked_positions(state),
        "changed": changed,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
