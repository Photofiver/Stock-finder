"""Offline checks for the TOP50 candle scanner; no exchange order calls."""
import os
import threading
import time
import unittest
from collections import Counter
from unittest.mock import patch

os.environ["LIVE_UNIVERSE_TOP_N"] = "50"
os.environ["LIVE_ENTRY_TOP_N"] = "50"
os.environ["BLOFIN_LIVE_ENABLED"] = "false"

import blofin_live_hourly as bot
import blofin_live_signal_watch as scan
import blofin_ma10_ma20_live as ma


class ParallelTop50Tests(unittest.TestCase):
    def test_configuration_and_entry_ranks(self):
        self.assertEqual(bot.TOP_N, 50)
        self.assertEqual(ma.EXTENDED_ENTRY_RANK_MAX, 50)
        self.assertIsNone(ma.rank_entry_rejection(3, {}, "C3", 100))
        self.assertIsNone(ma.rank_entry_rejection(50, {}, "C50", 100))
        self.assertIsNotNone(ma.rank_entry_rejection(51, {}, "C51", 100))
        state = {"ma_top2_watchlist": {
            "C1": {"last_rank": 1, "last_top2_ms": 100, "entered_once": False},
        }}
        self.assertIsNone(ma.rank_entry_rejection(1, state, "C1", 100))
        state["ma_top2_watchlist"]["C1"]["entered_once"] = True
        self.assertIsNotNone(ma.rank_entry_rejection(1, state, "C1", 100))

    def test_fifty_candles_fetched_once_in_parallel(self):
        symbols = [f"TEST{i}-USDT" for i in range(50)]
        signal_close = 1_790_000_000_000
        concurrent = 0
        highest = 0
        seen = Counter()
        lock = threading.Lock()

        def fake_fetch(inst):
            nonlocal concurrent, highest
            with lock:
                concurrent += 1
                highest = max(highest, concurrent)
                seen[inst] += 1
            time.sleep(0.01)
            with lock:
                concurrent -= 1
            return [{"close_ms": signal_close, "c": 1.0}]

        with patch.object(bot, "fetch_signal_bars", side_effect=fake_fetch), \
             patch.object(bot, "bar_close_ms", side_effect=lambda bar: bar["close_ms"]):
            cached = scan.wait_for_confirmed_signal_close(symbols, signal_close)
        self.assertEqual(set(cached), set(symbols))
        self.assertEqual(sum(seen.values()), 50)
        self.assertGreater(highest, 1)
        self.assertLessEqual(highest, scan.CANDLE_CONFIRM_WORKERS)

    def test_stale_candle_retried_before_reuse(self):
        signal_close = 1_790_000_000_000
        seen = Counter()

        def fake_fetch(inst):
            seen[inst] += 1
            close = signal_close - 300_000 if inst == "X-USDT" and seen[inst] == 1 else signal_close
            return [{"close_ms": close, "c": 1.0}]

        with patch.object(bot, "fetch_signal_bars", side_effect=fake_fetch), \
             patch.object(bot, "bar_close_ms", side_effect=lambda bar: bar["close_ms"]), \
             patch.object(scan, "CANDLE_CONFIRM_DELAY_SEC", 0):
            cached = scan.wait_for_confirmed_signal_close(["X-USDT", "Y-USDT"], signal_close)
        self.assertEqual(len(cached), 2)
        self.assertEqual(seen["X-USDT"], 2)
        self.assertEqual(seen["Y-USDT"], 1)

    def test_unconfirmed_symbol_is_skipped_not_traded(self):
        signal_close = 1_790_000_000_000
        def fake_fetch(inst):
            return [{"close_ms": signal_close if inst == "FRESH" else signal_close - 300_000, "c": 1.0}]

        with patch.object(bot, "fetch_signal_bars", side_effect=fake_fetch), \
             patch.object(bot, "bar_close_ms", side_effect=lambda bar: bar["close_ms"]), \
             patch.object(scan, "CANDLE_CONFIRM_DELAY_SEC", 0):
            cached = scan.wait_for_confirmed_signal_close(["FRESH", "STALE"], signal_close)
        self.assertEqual(set(cached), {"FRESH"})

    def test_cached_bars_skip_second_market_fetch(self):
        signal_close = 1_790_000_000_000
        bars = [
            {"close_ms": signal_close - (20 - i) * 300_000,
             "c": 1.0 if i < 20 else 1.1, "rsi": 50}
            for i in range(21)
        ]
        with patch.object(bot, "fetch_signal_bars", side_effect=AssertionError("unexpected second fetch")), \
             patch.object(bot, "bar_close_ms", side_effect=lambda bar: bar["close_ms"]), \
             patch.object(bot, "stochastic_kd_series", return_value=([40.0] * 21, [30.0] * 21)), \
             patch.object(ma.sr, "confirmed_levels", return_value={}):
            result = ma.crossover(bot, "X-USDT", signal_close, bars=bars)
        self.assertEqual(result["side"], "LONG")


if __name__ == "__main__":
    unittest.main()
