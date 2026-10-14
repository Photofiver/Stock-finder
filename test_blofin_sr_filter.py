"""Regression tests for the read-only 0.3% S/R entry restriction."""
import unittest

import blofin_support_resistance as sr

BAR_MS = 300000


def example_bars(length=22):
    bars = [
        {"close_ms": (i+1)*BAR_MS, "h": 100.1, "l": 99.9, "c": 100.0}
        for i in range(length)
    ]
    bars[8]["h"] = 101.0
    bars[12]["l"] = 99.0
    return bars


class ConfirmedSupportResistanceTests(unittest.TestCase):
    def test_only_confirmed_swing_levels(self):
        bars = example_bars()
        levels = sr.confirmed_levels(bars, bars[-1]["close_ms"], BAR_MS)
        self.assertEqual(levels["resistance"], [101.0])
        self.assertEqual(levels["support"], [99.0])

    def test_long_near_resistance_blocks_and_far_allows(self):
        bars = example_bars()
        levels = sr.confirmed_levels(bars, bars[-1]["close_ms"], BAR_MS)
        blocked = sr.check_entry(levels, "LONG", 100.8)
        self.assertEqual(blocked["reason"], "NEAR_RESISTANCE")
        self.assertFalse(blocked["allowed"])
        self.assertAlmostEqual(blocked["distance_pct"], .1984127, places=5)
        self.assertTrue(sr.check_entry(levels, "LONG", 100.6)["allowed"])
        self.assertTrue(sr.check_entry(levels, "LONG", 101.2)["allowed"])

    def test_short_near_support_blocks_and_far_allows(self):
        bars = example_bars()
        levels = sr.confirmed_levels(bars, bars[-1]["close_ms"], BAR_MS)
        self.assertEqual(sr.check_entry(levels, "SHORT", 99.2)["reason"], "NEAR_SUPPORT")
        self.assertTrue(sr.check_entry(levels, "SHORT", 99.5)["allowed"])
        self.assertTrue(sr.check_entry(levels, "SHORT", 98.5)["allowed"])

    def test_preorder_quote_revalidated(self):
        bars = example_bars()
        levels = sr.confirmed_levels(bars, bars[-1]["close_ms"], BAR_MS)
        self.assertTrue(sr.check_entry(levels, "LONG", 100.5)["allowed"])
        self.assertFalse(sr.check_entry(levels, "LONG", 100.8)["allowed"])
        self.assertTrue(sr.check_entry(levels, "SHORT", 99.5)["allowed"])
        self.assertFalse(sr.check_entry(levels, "SHORT", 99.2)["allowed"])

    def test_unconfirmed_last_two_bars_and_future_do_not_leak(self):
        bars = example_bars()
        bars[-2]["h"] = 110  # only one future candle: NOT a confirmed pivot
        bars[-1]["l"] = 90  # no confirmation bars at all
        cutoff = bars[-1]["close_ms"]
        levels = sr.confirmed_levels(
            bars + [
                {"close_ms": cutoff + BAR_MS, "h": 120, "l": 80, "c": 100},
                {"close_ms": cutoff + 2*BAR_MS, "h": 130, "l": 70, "c": 100},
            ], cutoff, BAR_MS,
        )
        self.assertEqual(levels["resistance"], [101])
        self.assertEqual(levels["support"], [99])

    def test_gapped_candles_do_not_create_fake_pivot(self):
        bars = example_bars()
        bars[9]["close_ms"] += 1000
        levels = sr.confirmed_levels(bars, bars[-1]["close_ms"], BAR_MS)
        self.assertEqual(levels["resistance"], [])

    def test_missing_history_is_rejected(self):
        with self.assertRaises(ValueError):
            sr.confirmed_levels(example_bars(22)[-4:], 22*BAR_MS, BAR_MS)

    def test_missing_or_bad_prices_block_by_default(self):
        for levels in (None, {}, {"error": "candles unavailable"}):
            with self.subTest(levels=levels):
                self.assertFalse(sr.check_entry(levels, "LONG", 100)["allowed"])
        for price in (0, -1, "garbage", float("nan"), float("inf")):
            with self.subTest(price=price):
                self.assertFalse(sr.check_entry({"resistance": [], "support": []}, "LONG", price)["allowed"])

    def test_no_pivots_does_not_block_entries(self):
        levels = sr.confirmed_levels(
            [{"close_ms": (i+1)*BAR_MS, "h": 100.1, "l": 99.9} for i in range(22)],
            22*BAR_MS, BAR_MS,
        )
        self.assertTrue(sr.check_entry(levels, "LONG", 100)["allowed"])
        self.assertTrue(sr.check_entry(levels, "SHORT", 100)["allowed"])

    def test_exact_threshold_is_blocked(self):
        levels = {"resistance": [100.3], "support": [99.7]}
        self.assertFalse(sr.check_entry(levels, "LONG", 100)["allowed"])
        self.assertFalse(sr.check_entry(levels, "SHORT", 99.7/.997)["allowed"])


if __name__ == "__main__":
    unittest.main()
