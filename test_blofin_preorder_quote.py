"""No-exchange regression tests for stale quote refresh before LIVE entry."""
import unittest
from unittest.mock import patch

import blofin_live_hourly as bot


class EntryQuoteRefreshTests(unittest.TestCase):
    @staticmethod
    def ticker(timestamp, bid="100", ask="100.01"):
        return [{
            "instId": "AERO-USDT",
            "ts": str(timestamp),
            "bidPrice": bid,
            "askPrice": ask,
        }]

    def test_stale_aero_quote_refreshed_before_order_preflight(self):
        # Age 15.635s must not pass unchanged; the next fresh quote can pass.
        with (
            patch.object(bot, "market_get", side_effect=[
                self.ticker(84365), self.ticker(98000),
            ]) as request,
            patch.object(bot, "now_ms", return_value=100000),
            patch.object(bot.time, "sleep") as sleep,
        ):
            result = bot.checked_preorder_quote("AERO-USDT", "LONG", "100")

        self.assertEqual(result["quote_age_ms"], 2000)
        self.assertEqual(result["expected_fill"], "100.01")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(bot.MAX_ENTRY_QUOTE_AGE_MS, 15000)

    def test_stale_after_all_refreshes_stays_blocked(self):
        with (
            patch.object(bot, "market_get", return_value=self.ticker(84365)) as request,
            patch.object(bot, "now_ms", return_value=100000),
            patch.object(bot.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "stale market quote.*after 3 checks"):
                bot.checked_preorder_quote("AERO-USDT", "LONG", "100")

        self.assertEqual(request.call_count, 4)  # three tickers + book fallback
        self.assertEqual(sleep.call_count, 2)

    def test_fresh_book_unblocks_stale_ticker(self):
        book = [{
            "ts": "99000",
            "bids": [["99.99", "20"]],
            "asks": [["100.01", "10"]],
        }]
        with (
            patch.object(bot, "market_get", side_effect=[
                self.ticker(65000), self.ticker(65000),
                self.ticker(65000), book,
            ]) as request,
            patch.object(bot, "now_ms", return_value=100000),
            patch.object(bot.time, "sleep"),
        ):
            result = bot.checked_preorder_quote("AERO-USDT", "LONG", "100")
        self.assertEqual(request.call_count, 4)
        self.assertEqual(result["quote_source"], "order_book")
        self.assertEqual(result["quote_age_ms"], 1000)
        self.assertEqual(result["expected_fill"], "100.01")
        self.assertLessEqual(result["spread_pct"], 0.25)

    def test_stale_order_book_never_bypasses_age_guard(self):
        old_book = [{
            "ts": "70000", "bids": [["100", "10"]],
            "asks": [["100.01", "10"]],
        }]
        with (
            patch.object(bot, "market_get", side_effect=[
                self.ticker(65000), self.ticker(65000),
                self.ticker(65000), old_book,
            ]),
            patch.object(bot, "now_ms", return_value=100000),
            patch.object(bot.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "order book fallback unavailable"):
                bot.checked_preorder_quote("AERO-USDT", "LONG", "100")

    def test_fresh_book_does_not_bypass_price_gap_limit(self):
        book = [{
            "ts": "99000", "bids": [["100.4", "10"]],
            "asks": [["100.5", "10"]],
        }]
        with (
            patch.object(bot, "market_get", side_effect=[
                self.ticker(65000), self.ticker(65000),
                self.ticker(65000), book,
            ]),
            patch.object(bot, "now_ms", return_value=100000),
            patch.object(bot.time, "sleep"),
            patch.object(bot, "append_technical_event") as event,
        ):
            with self.assertRaisesRegex(RuntimeError, "adverse price move"):
                bot.checked_preorder_quote("AERO-USDT", "LONG", "100")
            event.assert_called_once()

    def test_fresh_quote_still_blocked_by_spread_guard(self):
        with (
            patch.object(bot, "market_get", side_effect=[
                self.ticker(84365), self.ticker(98000, bid="100", ask="101"),
            ]),
            patch.object(bot, "now_ms", return_value=100000),
            patch.object(bot.time, "sleep"),
            patch.object(bot, "append_technical_event") as event,
        ):
            with self.assertRaisesRegex(RuntimeError, "spread"):
                bot.checked_preorder_quote(
                    "AERO-USDT", "LONG", "100", enforce_adverse_gap=False,
                )
            event.assert_called_once()

    def test_fresh_quote_is_not_retried(self):
        with (
            patch.object(bot, "market_get", return_value=self.ticker(98000)) as request,
            patch.object(bot, "now_ms", return_value=100000),
            patch.object(bot.time, "sleep") as sleep,
        ):
            bot.checked_preorder_quote("AERO-USDT", "SHORT", "100")
        request.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
