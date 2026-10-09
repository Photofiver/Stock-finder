"""Offline regression tests: LIVE nearest scoring, strict volume gate and bankroll."""
import unittest
from unittest.mock import patch
from contextlib import contextmanager

import blofin_live_hourly as bot
import blofin_nearest_volume as nearest


def example_metrics():
    short = {
        "red_candle": False,
        "volume_higher_than_last_green": True,
        "short_body_max_1pct": True,
        "lower_wick_max_50pct": True,
        "return_4_bars_min_minus_2pct": True,
        "return_4_bars_max_1pct": True,
        "return_20_bars_max_10pct": True,
        "short_rsi_min_55": True,
        "short_macd_hist_delta_ok": True,
        "last_green_volume": 10,
    }
    long = {
        "green_candle": True,
        "volume_higher_than_last_red": True,
        "green_body_min_60pct": False,
        "rise_10_bars_min_2pct": True,
        "stoch_long_ok": True,
        "rsi_below_67": True,
        "last_red_volume": 10,
    }
    return short, long


class NearestLiveTests(unittest.TestCase):
    def test_score_matches_saved_json_checks_and_volume(self):
        bars = [{"o": 99, "c": 100, "v": 30} for _ in range(21)]
        short, long = example_metrics()
        with patch.object(bot, "short_entry_metrics", return_value=short), \
             patch.object(bot, "long_entry_metrics", return_value=long), \
             patch.object(bot, "detect_chart_patterns", return_value=[]), \
             patch.object(bot, "candle_color", return_value="GREEN"):
            options = nearest.score_options(bot, bars, 20)
        indexed = {x["side"]: x for x in options}
        self.assertEqual(indexed["LONG"]["score"], "6/7")
        self.assertEqual(indexed["LONG"]["missing"], ["BODY_GE_60PCT"])
        self.assertTrue(indexed["LONG"]["volume_rule_passed"])
        self.assertEqual(indexed["SHORT"]["score"], "9/10")
        self.assertFalse(indexed["SHORT"]["volume_rule_passed"])

    def test_candle_direction_and_higher_opposite_volume_are_mandatory(self):
        example = {
            "color": "GREEN", "volume_vs_opposite_ratio": 2.0,
            "filter_metrics": {
                "volume_higher_than_last_red": True,
                "volume_higher_than_last_green": True,
            },
        }
        self.assertTrue(nearest.volume_snapshot_passes(example, "LONG"))
        self.assertFalse(nearest.volume_snapshot_passes(example, "SHORT"))
        example["volume_vs_opposite_ratio"] = 1.0
        self.assertFalse(nearest.volume_snapshot_passes(example, "LONG"))

    def test_ranks_only_volume_passed_in_fallback_order(self):
        close_ms = 90000000
        series = lambda inst: [
            {"ts": close_ms - bot.SIGNAL_MS, "inst": inst}
            for _ in range(40)
        ]
        options = {
            "A": [{
                "side": "SHORT", "score": "8/10", "score_ratio": .8,
                "missing": ["A", "B"], "missing_count": 2,
                "volume_rule_passed": True, "volume_advantage_pct": 30,
            }],
            "B": [{
                "side": "LONG", "score": "6/7", "score_ratio": 6/7,
                "missing": ["A"], "missing_count": 1,
                "volume_rule_passed": True, "volume_advantage_pct": 10,
            }],
            "C": [{
                "side": "SHORT", "score": "10/10", "score_ratio": 1,
                "missing": [], "missing_count": 0,
                "volume_rule_passed": False, "volume_advantage_pct": -1,
            }],
        }
        with patch.object(bot, "fetch_signal_bars", side_effect=lambda inst: series(inst)), \
             patch.object(bot, "bar_close_ms", return_value=close_ms), \
             patch.object(bot, "now_ms", return_value=close_ms + 65000), \
             patch.object(nearest, "score_options", side_effect=lambda _bot, bars, i: [
                dict(v) for v in options[bars[-1]["inst"]]
             ]):
            state = {"last_processed_close_ms": {}}
            chosen = nearest.select_signals(state, ["A", "B", "C"])
        self.assertEqual(chosen, [(2, "B", "LONG", close_ms), (1, "A", "SHORT", close_ms)])
        self.assertEqual(state["last_pretrade_ranking"]["strategy"], "NEAREST_VOLUME")
        self.assertEqual(len(state["last_pretrade_ranking"]["eligible_entry_order"]), 2)

    def test_no_volume_no_trade(self):
        close_ms = 90000000
        with patch.object(bot, "fetch_signal_bars", return_value=[
            {"ts": close_ms - bot.SIGNAL_MS} for _ in range(40)
        ]), patch.object(bot, "bar_close_ms", return_value=close_ms), \
             patch.object(bot, "now_ms", return_value=close_ms + 65000), \
             patch.object(nearest, "score_options", return_value=[{
                "side": "LONG", "score": "7/7", "score_ratio": 1,
                "missing": [], "missing_count": 0,
                "volume_rule_passed": False, "volume_advantage_pct": -5,
             }]):
            state = {"last_processed_close_ms": {}}
            self.assertEqual(nearest.select_signals(state, ["A"]), [])
            self.assertEqual(state["last_pretrade_ranking"]["eligible_entry_order"], [])

    def test_rebase_to_10_once_preserves_old_history_and_risk_stop(self):
        state = {
            "bankroll_start_usdt": 40,
            "bankroll_pnl_baseline_usdt": -2.8,
            "realized_pnl_usdt": -3.4,
            "trade_history": [{"inst": "OLD", "net_pnl_usdt": -1}],
            "risk_stop_triggered": False,
        }
        with patch.object(bot, "get_tracked_positions", return_value={}), \
             patch.object(bot, "now_ms", return_value=123):
            self.assertTrue(nearest.initialize_bankroll(state))
            self.assertFalse(nearest.initialize_bankroll(state))
        self.assertEqual(state["bankroll_start_usdt"], "10")
        self.assertEqual(state["bankroll_pnl_baseline_usdt"], "-3.4")
        self.assertEqual(bot.current_live_bankroll(state), bot.d("10"))
        self.assertEqual(len(state["trade_history"]), 1)
        self.assertEqual(len(state["bankroll_rebase_history"]), 1)

    def test_preserve_existing_tp_sl_and_hold(self):
        self.assertEqual(bot.TAKE_PROFIT_PCT, bot.d("0.01"))
        self.assertEqual(bot.HARD_SL_PCT, bot.d("0.01"))
        self.assertEqual(bot.HOLD_MINUTES, 60)


def valid_preorder_snapshot(side):
    return {
        "color": "GREEN" if side == "LONG" else "RED",
        "close": 100.0,
        "volume_vs_opposite_ratio": 2.0,
        "filter_metrics": {
            "volume_higher_than_last_red": True,
            "volume_higher_than_last_green": True,
        },
    }


@contextmanager
def offline_fallback_broker(close_ms, quote, order, positions=None):
    # Patch every broker read/write. These tests cannot reach BloFin.
    with patch.object(bot, "risk_stop_active", return_value=False), \
         patch.object(bot, "get_tracked_positions", return_value=positions or {}), \
         patch.object(bot, "get_open_positions", return_value=[]), \
         patch.object(bot, "current_live_bankroll", return_value=bot.d("10")), \
         patch.object(bot, "get_available_usdt", return_value=bot.d("25")), \
         patch.object(bot, "now_ms", return_value=close_ms + 70000), \
         patch.object(bot, "build_signal_snapshot",
                      side_effect=lambda _i, side, _ms, _rank: valid_preorder_snapshot(side)), \
         patch.object(bot, "checked_preorder_quote", side_effect=quote) as quote_mock, \
         patch.object(bot, "place_live_trade_multi", side_effect=order) as order_mock, \
         patch.object(bot, "append_technical_event") as events:
        yield quote_mock, order_mock, events


class FallbackExecutionTests(unittest.TestCase):
    SIGNAL_CLOSE = 90000000

    @classmethod
    def candidates(cls):
        return [
            (5, "ZK-USDT", "SHORT", cls.SIGNAL_CLOSE),
            (4, "BAT-USDT", "LONG", cls.SIGNAL_CLOSE),
            (7, "Q-USDT", "SHORT", cls.SIGNAL_CLOSE),
        ]

    def test_rejected_quote_falls_back_and_opens_only_one_trade(self):
        def quote(inst, side, close):
            if inst == "ZK-USDT":
                raise RuntimeError("adverse price move 0.458% > 0.25%")
            return {"expected_fill": "100"}

        def order(state, candidate, tickers, instruments, budget, label):
            return {"inst": candidate[1], "notional_usdt": "9.9"}

        state = {"last_pretrade_ranking": {"entry_attempts": []}}
        with offline_fallback_broker(self.SIGNAL_CLOSE, quote, order) as (q, orders, events):
            actual = nearest.execute_with_fallback(
                state, self.candidates(), {}, {}
            )
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]["inst"], "BAT-USDT")
        self.assertEqual(orders.call_count, 1)
        self.assertEqual(orders.call_args.args[1][1], "BAT-USDT")
        self.assertEqual(orders.call_args.args[4], bot.d("10"))
        self.assertEqual(q.call_count, 2)
        self.assertEqual(
            [v["status"] for v in state["last_pretrade_ranking"]["entry_attempts"]],
            ["PRE_ORDER_REJECTED", "EXECUTED"],
        )
        self.assertEqual(state["last_pretrade_ranking"]["executed_candidate"]["priority"], 2)
        self.assertNotIn("_nearest_volume_order_post_started", state)

    def test_all_quotes_rejected_no_broker_orders(self):
        def quote(*args):
            raise RuntimeError("market quote too far from signal close")
        state = {"last_pretrade_ranking": {}}
        with offline_fallback_broker(self.SIGNAL_CLOSE, quote, None) as (q, orders, _):
            self.assertEqual(nearest.execute_with_fallback(
                state, self.candidates(), {}, {}
            ), [])
        self.assertEqual(q.call_count, 3)
        orders.assert_not_called()
        self.assertEqual(
            len(state["last_pretrade_ranking"]["entry_attempts"]), 3
        )

    def test_preorder_trade_function_rejection_can_fall_back(self):
        def order(state, candidate, tickers, instruments, budget, label):
            if candidate[1] == "ZK-USDT":
                raise RuntimeError("minimum lot exceeds 10 USDT")
            return {"inst": candidate[1], "notional_usdt": "9.7"}
        state = {"last_pretrade_ranking": {}}
        with offline_fallback_broker(self.SIGNAL_CLOSE, lambda *_: {}, order) as (_, orders, _):
            result = nearest.execute_with_fallback(
                state, self.candidates(), {}, {}
            )
        self.assertEqual(result[0]["inst"], "BAT-USDT")
        self.assertEqual(orders.call_count, 2)
        self.assertEqual(
            [a["status"] for a in state["last_pretrade_ranking"]["entry_attempts"]],
            ["PRE_ORDER_REJECTED", "EXECUTED"],
        )

    def test_post_attempt_exception_stops_fallback_no_duplicate_orders(self):
        def order(state, candidate, tickers, instruments, budget, label):
            state["_nearest_volume_order_post_started"] = True
            raise TimeoutError("BloFin market order response timed out")
        state = {"last_pretrade_ranking": {}}
        with offline_fallback_broker(self.SIGNAL_CLOSE, lambda *_: {}, order) as (_, orders, _):
            result = nearest.execute_with_fallback(
                state, self.candidates(), {}, {}
            )
        self.assertEqual(result, [])
        self.assertEqual(orders.call_count, 1)
        self.assertEqual(
            state["last_pretrade_ranking"]["entry_attempts"][0]["status"],
            "ORDER_STATUS_UNCERTAIN_STOP",
        )
        self.assertNotIn("_nearest_volume_order_post_started", state)

    def test_uncertain_position_after_market_post_stops_fallback(self):
        def order(state, candidate, tickers, instruments, budget, label):
            state["_nearest_volume_order_post_started"] = True
            return None
        state = {"last_pretrade_ranking": {}}
        with offline_fallback_broker(self.SIGNAL_CLOSE, lambda *_: {}, order) as (_, orders, _):
            result = nearest.execute_with_fallback(
                state, self.candidates(), {}, {}
            )
        self.assertEqual(result, [])
        self.assertEqual(orders.call_count, 1)
        self.assertEqual(
            state["last_pretrade_ranking"]["entry_attempts"][0]["status"],
            "ORDER_ATTEMPT_UNCONFIRMED_STOP",
        )

    def test_existing_account_position_budget_and_one_open_per_coin(self):
        account_pos = {"instId": "ZK-USDT"}
        tracked = {"ZK-USDT": {"notional_usdt": "3"}}
        state = {"last_pretrade_ranking": {}}
        with patch.object(bot, "risk_stop_active", return_value=False), \
             patch.object(bot, "get_tracked_positions", return_value=tracked), \
             patch.object(bot, "get_open_positions", return_value=[account_pos]), \
             patch.object(bot, "current_live_bankroll", return_value=bot.d("10")), \
             patch.object(bot, "get_available_usdt", return_value=bot.d("25")), \
             patch.object(bot, "now_ms", return_value=self.SIGNAL_CLOSE + 70000), \
             patch.object(bot, "build_signal_snapshot",
                          side_effect=lambda _i, side, _ms, _r: valid_preorder_snapshot(side)), \
             patch.object(bot, "checked_preorder_quote", return_value={}), \
             patch.object(bot, "place_live_trade_multi",
                          return_value={"inst": "BAT-USDT", "notional_usdt": "7"}) as orders:
            result = nearest.execute_with_fallback(state, self.candidates(), {}, {})
        self.assertEqual(result[0]["inst"], "BAT-USDT")
        self.assertEqual(orders.call_args.args[4], bot.d("7"))
        self.assertEqual(
            state["last_pretrade_ranking"]["entry_attempts"][0]["status"],
            "SKIPPED_ALREADY_OPEN",
        )


if __name__ == "__main__":
    unittest.main()
