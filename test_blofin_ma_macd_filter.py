"""Offline regression tests: LIVE MACD(12,26,9) histogram entry gate."""
import unittest
from unittest.mock import MagicMock, patch

import blofin_ma10_ma20_live as ma


def signal(side, previous, current):
    return {
        "inst": "TEST-USDT", "side": side, "signal_close_ms": 300000,
        "close": 100.1, "ma10": 100.0, "sma_separation_pct": 0.1,
        "stoch_k": 40 if side == "LONG" else 60,
        "stoch_d": 60 if side == "LONG" else 40,
        "rsi14": 55 if side == "LONG" else 45,
        "macd_hist_previous": previous, "macd_hist": current,
        "sr_levels": {"support": [], "resistance": []},
    }


class LiveMacdFilterTests(unittest.TestCase):
    def test_accepts_only_growing_green_long_and_growing_red_short(self):
        for side, old, now in (("LONG", 0.001, 0.002),
                               ("LONG", -0.001, 0.001),
                               ("SHORT", -0.001, -0.002),
                               ("SHORT", 0.001, -0.001)):
            with self.subTest(side=side, previous=old, current=now):
                self.assertIsNone(ma.entry_macd_rejection(signal(side, old, now)))

    def test_rejects_shrinking_flat_and_wrong_color(self):
        cases = [
            ("LONG", 0.002, 0.001),
            ("LONG", 0.001, 0.001),
            ("LONG", -0.003, -0.002),
            ("LONG", -0.001, 0.0),
            ("SHORT", -0.002, -0.001),
            ("SHORT", -0.001, -0.001),
            ("SHORT", 0.003, 0.002),
            ("SHORT", 0.001, 0.0),
        ]
        for side, old, now in cases:
            with self.subTest(side=side, previous=old, current=now):
                self.assertIsNotNone(ma.entry_macd_rejection(signal(side, old, now)))

    def test_missing_or_non_finite_histogram_fails_closed(self):
        for value in (None, float("nan"), float("inf"), float("-inf"), "bad"):
            for side in ("LONG", "SHORT"):
                with self.subTest(side=side, bad=value):
                    self.assertIn("MACD_HIST_UNAVAILABLE",
                                  ma.entry_macd_rejection(signal(side, 0.001, value)))
        missing = signal("LONG", 0.001, 0.002)
        del missing["macd_hist_previous"]
        self.assertIn("MACD_HIST_UNAVAILABLE", ma.entry_macd_rejection(missing))

    def test_blocked_signal_does_not_call_broker(self):
        bot = MagicMock()
        for side, prev, cur in (("LONG", 0.002, 0.001),
                                ("SHORT", -0.002, -0.001)):
            with self.subTest(side=side):
                self.assertFalse(ma.open_position(
                    bot, {}, signal(side, prev, cur), 51, {},
                ))
        bot.private_request.assert_not_called()
        bot.get_open_positions.assert_not_called()
        bot.risk_stop_active.assert_not_called()

    def test_crossover_records_closed_candle_histogram(self):
        bars = [
            {"c": 10.0 if i < 20 else 10.2,
             "h": 10.3, "l": 9.8, "rsi": 55,
             "macd_hist": 0.001 if i < 20 else 0.002,
             "close_ms": (i + 1) * 300000}
            for i in range(21)
        ]
        bot = MagicMock()
        bot.bar_close_ms.side_effect = lambda bar: bar["close_ms"]
        bot.stochastic_kd_series.return_value = ([40], [60])
        bot.SIGNAL_LABEL = "5m"
        bot.SIGNAL_MINUTES = 5
        result = ma.crossover(bot, "TEST-USDT", 21 * 300000, bars=bars)
        self.assertEqual(result["side"], "LONG")
        self.assertEqual(result["macd_hist_previous"], 0.001)
        self.assertEqual(result["macd_hist"], 0.002)

    def test_scan_skips_bad_macd_then_tries_next_rank(self):
        bot = MagicMock()
        bot.SIGNAL_MINUTES = 5
        bot.SIGNAL_LABEL = "5m"
        bot.now_ms.return_value = 300005
        bot.get_tracked_positions.side_effect = lambda s: s["positions"]
        bot.get_open_positions.return_value = []
        bot.risk_stop_active.return_value = False
        names = [f"FILL-{i}-USDT" for i in range(50)] + ["BAD-USDT", "GOOD-USDT"]
        state = {"positions": {}, "last_scan_close_ms": 0}
        opened = []

        def cross(b, inst, close_ms, bars=None):
            if inst == "BAD-USDT":
                return {**signal("LONG", 0.003, 0.002),
                        "inst": inst, "signal_close_ms": close_ms}
            if inst == "GOOD-USDT":
                return {**signal("LONG", 0.001, 0.002),
                        "inst": inst, "signal_close_ms": close_ms}
            return {"inst": inst, "side": None, "signal_close_ms": close_ms}

        def opened_stub(b, s, sig, rank, insts):
            opened.append((sig["inst"], rank))
            return True

        with patch.object(ma, "crossover", side_effect=cross), \
             patch.object(ma, "open_position", side_effect=opened_stub):
            ma.run(bot, state, names, {}, {}, 300000,
                   lambda insts, close: {inst: [] for inst in insts})
        self.assertEqual(opened, [("GOOD-USDT", 52)])
        rejected = [
            event for event in state["ma_cross_last_scan"]["events"]
            if event.get("status") == "MACD_ENTRY_BLOCKED_TRY_NEXT"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["inst"], "BAD-USDT")
        self.assertEqual(rejected[0]["macd_hist_previous"], 0.003)
        self.assertEqual(rejected[0]["macd_hist"], 0.002)


if __name__ == "__main__":
    unittest.main()
