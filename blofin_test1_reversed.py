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
        if BROKER_ID:
            body.setdefault("brokerId", BROKER_ID)
    return _original_private_request(method, path, params=params, body=body)


bot.private_request = private_request_with_broker


def reversed_volume_signal(bars, i):
    if i < 0:
        return None
    green_v, red_v = bot.last_green_red_volume(bars, i)
    if green_v is None or red_v is None:
        return None

    # TEST 1 ODWRÓCONY:
    # latest GREEN Volume > latest RED Volume => LONG
    # latest RED Volume > latest GREEN Volume => SHORT
    if green_v > red_v:
        return "LONG"
    if red_v > green_v:
        return "SHORT"
    return None


bot.volume_flip_signal = reversed_volume_signal


def setup_reversed_test():
    bot.require_live_enabled()
    state = bot.load_state()

    # Finalise TEST 1 cleanly: sync anything already closed, then close any
    # positions that are still open so the reversed test starts from a clean slate.
    bot.sync_all_tracked_positions(state)
    for inst in list(bot.get_tracked_positions(state)):
        bot._run_for_tracked_position(
            state, inst, bot.close_tracked_position, "TEST 1 finished - reset for reversed test"
        )

    # Sync once more in case close history became available during the close calls.
    bot.sync_all_tracked_positions(state)

    baseline = float(state.get("realized_pnl_usdt", 0.0))
    state["bankroll_start_usdt"] = 10.0
    state["bankroll_pnl_baseline_usdt"] = baseline
    state["risk_stop_triggered"] = False
    state.pop("risk_stop_triggered_ms", None)
    state.pop("risk_stop_bankroll_usdt", None)
    state.pop("risk_stop_threshold_usdt", None)
    state["last_processed_close_ms"] = {}
    state["test1_reversed"] = {
        "name": "TEST 1 ODWRÓCONY",
        "status": "running",
        "starting_bankroll_usdt": 10.0,
        "max_drawdown_pct": 10,
        "stop_threshold_usdt": 9.0,
        "signal_minutes": 5,
        "scan_count_target": 12,
        "signal_source": "BloFin native 5m confirmed candles",
        "rule": "latest GREEN Volume > latest RED Volume => LONG; latest RED Volume > latest GREEN Volume => SHORT",
        "started_from_realized_pnl_baseline_usdt": baseline,
        "started_trade_total": int(state.get("trades", {}).get("total", 0)),
        "started_history_count": len(state.get("trade_history", [])),
        "started_at_ms": bot.now_ms(),
    }
    if isinstance(state.get("test1"), dict):
        state["test1"]["status"] = "finished"

    bot.save_state(state)
    print("TEST 1 ODWRÓCONY setup complete")


if __name__ == "__main__":
    mode = os.getenv("TEST1_REVERSED_MODE", "run").strip().lower()
    if mode == "setup":
        setup_reversed_test()
    else:
        bot.main()
