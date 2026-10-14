"""Regression tests: RSI(14) >75 blocks LIVE SMA entries without affecting existing exits."""
import unittest
from unittest.mock import MagicMock, patch

import blofin_ma10_ma20_live as ma


class Rsi75LiveEntryTests(unittest.TestCase):
    def test_rsi_threshold_applies_to_both_directions(self):
        for side in ("LONG", "SHORT"):
            for rsi in (0, 45, 74.99, 75.0):
                with self.subTest(side=side, rsi=rsi):
                    self.assertIsNone(ma.entry_rsi_rejection({"side": side, "rsi14": rsi}))
            for rsi in (75.0001, 79.03, 100.0):
                with self.subTest(side=side, rsi=rsi):
                    self.assertIn("RSI14_OVER_75", ma.entry_rsi_rejection({"side": side, "rsi14": rsi}))

    def test_missing_invalid_or_non_finite_rsi_fails_closed(self):
        for rsi in (None, "oops", float("nan"), float("inf"), -1.0, 101.0):
            with self.subTest(rsi=rsi):
                self.assertIn("RSI14_UNAVAILABLE", ma.entry_rsi_rejection({"rsi14": rsi}))

    def test_blocked_entry_never_queries_broker(self):
        bot = MagicMock()
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                self.assertFalse(ma.open_position(bot, {}, {
                    "inst": "Q-USDT", "side": side, "rsi14": 79.03,
                }, 1, {}))
        bot.private_request.assert_not_called()
        bot.risk_stop_active.assert_not_called()
        bot.get_open_positions.assert_not_called()

    def test_rsi_75_reaches_existing_risk_stop_gate(self):
        bot = MagicMock()
        bot.risk_stop_active.return_value = True
        self.assertFalse(ma.open_position(bot, {}, {
            "inst": "Q-USDT", "side": "LONG", "rsi14": 75.0,
            "close": 100.5, "ma10": 100.0,
            "sr_levels": {"resistance": [], "support": []},
        }, 1, {}))
        bot.risk_stop_active.assert_called_once()

    def test_crossover_uses_rsi_from_confirmed_bar(self):
        bars = [
            {"c": 10.0 if i < 20 else 12.0,
             "close_ms": (i + 1) * 300000,
             "rsi": 79.03 if i == 20 else 55.0}
            for i in range(21)
        ]
        bot = MagicMock()
        bot.SIGNAL_LABEL = "5m"
        bot.bar_close_ms.side_effect = lambda row: row["close_ms"]
        signal = ma.crossover(bot, "Q-USDT", 21 * 300000, bars=bars)
        self.assertEqual(signal["side"], "LONG")
        self.assertEqual(signal["rsi14"], 79.03)
        self.assertIn("RSI14_OVER_75", ma.entry_rsi_rejection(signal))

    def test_scan_skips_over_75_then_tries_next_cross(self):
        state = {"positions": {}, "last_scan_close_ms": 0}
        bot = MagicMock()
        bot.SIGNAL_MINUTES = 5
        bot.SIGNAL_LABEL = "5m"
        bot.get_tracked_positions.side_effect = lambda s: s["positions"]
        bot.get_open_positions.return_value = []
        bot.risk_stop_active.return_value = False
        bot.now_ms.return_value = 300005
        called = []
        def fake_cross(b, inst, expected_close_ms):
            return {
                "inst": inst, "side": "LONG",
                "rsi14": 79.0 if inst == "OVER-USDT" else 55.0,
                "close": 100.5, "ma10": 100.0,
                "sr_levels": {"resistance": [], "support": []},
                "signal_close_ms": expected_close_ms
            }
        def fake_open(b, s, signal, rank, instruments):
            called.append(signal["inst"])
            return True
        with patch.object(ma, "crossover", side_effect=fake_cross), \
             patch.object(ma, "open_position", side_effect=fake_open):
            ma.run(bot, state, ["OVER-USDT", "OK-USDT"], {}, {}, 300000,
                   lambda insts, closed: None)
        self.assertEqual(called, ["OK-USDT"])
        scan = state["ma_cross_last_scan"]
        self.assertEqual(scan["status"], "OPENED")
        self.assertEqual(scan["selected"]["inst"], "OK-USDT")
        self.assertTrue(any(e.get("status") == "RSI_ENTRY_BLOCKED_TRY_NEXT"
                            and e.get("inst") == "OVER-USDT" for e in scan["events"]))


if __name__ == "__main__":
    unittest.main()
