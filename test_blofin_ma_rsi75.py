"""Regression tests: current LIVE RSI 50..75 LONG and 0..50 SHORT."""
import unittest
from unittest.mock import MagicMock, patch

import blofin_ma10_ma20_live as ma


def signal(side="LONG", rsi=55.0):
    return {
        "inst": "Q-USDT", "side": side, "rsi14": rsi,
        "signal_close_ms": 300000,
        "close": 100.5 if side == "LONG" else 99.5,
        "ma10": 100.0,
        "sma_separation_pct": 0.05,
        "stoch_k": 40 if side == "LONG" else 60,
        "stoch_d": 60 if side == "LONG" else 40,
        "macd_hist_previous": 0.001 if side == "LONG" else -0.001,
        "macd_hist": 0.003 if side == "LONG" else -0.003,
        "sr_levels": {"resistance": [], "support": []},
    }


class Rsi75LiveEntryTests(unittest.TestCase):
    def test_rsi_long_50_to_75_and_short_0_to_50(self):
        for side, allowed, blocked, rejection in (
            ("LONG", (50, 50.01, 55, 74.99, 75.0), (0, 45, 49.99),
             "RSI14_LONG_BELOW_50"),
            ("SHORT", (0, 25, 49.99, 50), (50.01, 65, 74.99, 75.0),
             "RSI14_SHORT_ABOVE_50"),
        ):
            for rsi in allowed:
                with self.subTest(side=side, allowed=rsi):
                    self.assertIsNone(ma.entry_rsi_rejection(signal(side, rsi)))
            for rsi in blocked:
                with self.subTest(side=side, rejected=rsi):
                    self.assertIn(rejection, ma.entry_rsi_rejection(signal(side, rsi)))
        for side in ("LONG", "SHORT"):
            for rsi in (75.0001, 79.03, 100.0):
                with self.subTest(side=side, over75=rsi):
                    self.assertIn("RSI14_OVER_75", ma.entry_rsi_rejection(signal(side, rsi)))

    def test_missing_invalid_or_non_finite_rsi_fails_closed(self):
        for rsi in (None, "oops", float("nan"), float("inf"), -1.0, 101.0):
            with self.subTest(rsi=rsi):
                self.assertIn("RSI14_UNAVAILABLE", ma.entry_rsi_rejection(signal(rsi=rsi)))

    def test_blocked_entry_never_queries_broker(self):
        bot = MagicMock()
        for side, rsi in (("LONG", 79.03), ("SHORT", 79.03),
                          ("LONG", 49), ("SHORT", 51)):
            with self.subTest(side=side, rsi=rsi):
                self.assertFalse(ma.open_position(
                    bot, {}, signal(side, rsi), 51, {},
                ))
        bot.private_request.assert_not_called()
        bot.risk_stop_active.assert_not_called()
        bot.get_open_positions.assert_not_called()

    def test_rsi_75_reaches_existing_risk_stop_gate(self):
        bot = MagicMock()
        bot.risk_stop_active.return_value = True
        bot.get_tracked_positions.return_value = {}
        bot.get_open_positions.return_value = []
        self.assertFalse(ma.open_position(
            bot, {}, signal("LONG", 75), 51, {},
        ))
        bot.risk_stop_active.assert_called_once()
        bot.private_request.assert_not_called()

    def test_crossover_uses_rsi_from_confirmed_bar(self):
        bars = [
            {"c": 10.0 if i < 20 else 12.0,
             "h": 10.1 if i < 20 else 12.1,
             "l": 9.9 if i < 20 else 11.9,
             "close_ms": (i + 1) * 300000,
             "rsi": 79.03 if i == 20 else 55.0,
             "macd_hist": 0.003 if i == 20 else 0.001}
            for i in range(21)
        ]
        bot = MagicMock()
        bot.SIGNAL_LABEL = "5m"
        bot.SIGNAL_MINUTES = 5
        bot.bar_close_ms.side_effect = lambda row: row["close_ms"]
        bot.stochastic_kd_series.return_value = ([40], [60])
        cross = ma.crossover(bot, "Q-USDT", 21 * 300000, bars=bars)
        self.assertEqual(cross["side"], "LONG")
        self.assertEqual(cross["rsi14"], 79.03)
        self.assertIn("RSI14_OVER_75", ma.entry_rsi_rejection(cross))

    def test_scan_skips_over_75_then_tries_next_allowed_rank(self):
        state = {"positions": {}, "last_scan_close_ms": 0}
        bot = MagicMock()
        bot.SIGNAL_MINUTES = 5
        bot.SIGNAL_LABEL = "5m"
        bot.get_tracked_positions.side_effect = lambda s: s["positions"]
        bot.get_open_positions.return_value = []
        bot.risk_stop_active.return_value = False
        bot.now_ms.return_value = 300005
        rank_51, rank_52 = "OVER-USDT", "OK-USDT"
        names = [f"FLAT{i}-USDT" for i in range(1, 51)] + [rank_51, rank_52]
        called = []

        def fake_cross(b, inst, expected_close_ms, bars=None):
            if inst not in (rank_51, rank_52):
                return {"inst": inst, "side": None, "signal_close_ms": expected_close_ms}
            return {
                **signal("LONG", 79.0 if inst == rank_51 else 55.0),
                "inst": inst,
                "signal_close_ms": expected_close_ms,
            }

        def fake_open(b, s, data, rank, instruments):
            called.append((data["inst"], rank))
            return True

        with patch.object(ma, "crossover", side_effect=fake_cross), \
             patch.object(ma, "open_position", side_effect=fake_open):
            ma.run(
                bot, state, names, {}, {},
                300000, lambda insts, closed: {inst: [] for inst in insts},
            )
        self.assertEqual(called, [(rank_52, 52)])
        scan = state["ma_cross_last_scan"]
        self.assertEqual(scan["status"], "OPENED")
        self.assertEqual(scan["selected"]["inst"], rank_52)
        self.assertTrue(any(
            e.get("status") == "RSI_ENTRY_BLOCKED_TRY_NEXT"
            and e.get("inst") == rank_51 for e in scan["events"]
        ))


if __name__ == "__main__":
    unittest.main()
