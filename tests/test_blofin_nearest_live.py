"""Offline regression tests: LIVE nearest scoring, strict volume gate and bankroll."""
import unittest
from unittest.mock import patch

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

    def test_ranks_only_volume_passed_and_selects_one(self):
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
        self.assertEqual(chosen, [(2, "B", "LONG", close_ms)])
        self.assertEqual(state["last_pretrade_ranking"]["strategy"], "NEAREST_VOLUME")
        self.assertEqual(len(state["last_pretrade_ranking"]["eligible_entry_order"]), 1)

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


if __name__ == "__main__":
    unittest.main()
