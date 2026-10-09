"""Offline, credential-free checks for TOP7 ranking and the market-entry quote guard."""
import unittest
from unittest.mock import patch

import blofin_live_hourly as bot
import blofin_selection_rank as ranking


class RankingAndQuoteGuardTests(unittest.TestCase):
    def test_higher_quality_signal_is_processed_first(self):
        candidates = [
            (7, "ZK-USDT", "LONG", 12345),
            (4, "MAGIC-USDT", "LONG", 12345),
        ]
        scores = {
            ("ZK-USDT", "LONG"): {"score": -1},
            ("MAGIC-USDT", "LONG"): {"score": 6},
        }
        result = ranking.rank_signals(candidates, scores)
        self.assertEqual(result[0][1], "MAGIC-USDT")
        self.assertEqual(set(result), set(candidates))  # does not add a rejected coin

    def test_rsi_and_stochastic_saturation_is_penalised(self):
        previous = {"c": 0.01323, "macd_hist": -0.000017}
        now = {"o": 0.01323, "c": 0.013372, "rsi": 68.82, "macd_hist": -0.000008}
        score = ranking.score_at_close([previous, now], "LONG", 1, {
            "stoch_k": 94.72, "stoch_d": 82.18,
        })
        self.assertIn("rsi_overbought_warning", [r["factor"] for r in score["factors"]])
        self.assertIn("stochastic_high_warning", [r["factor"] for r in score["factors"]])

    def test_zk_adverse_entry_price_is_rejected(self):
        close = "0.013372"
        quote = [{
            "instId": "ZK-USDT", "bidPrice": "0.013490",
            "askPrice": "0.013493", "ts": "1999000",
        }]
        with patch.object(bot, "market_get", return_value=quote), \
             patch.object(bot, "now_ms", return_value=2000000), \
             patch.object(bot, "append_technical_event"):
            with self.assertRaisesRegex(RuntimeError, "adverse price move"):
                bot.checked_preorder_quote("ZK-USDT", "LONG", close)

    def test_reasonable_quote_is_allowed(self):
        quote = [{
            "instId": "ZK-USDT", "bidPrice": "0.013380",
            "askPrice": "0.013385", "ts": "1999000",
        }]
        with patch.object(bot, "market_get", return_value=quote), \
             patch.object(bot, "now_ms", return_value=2000000), \
             patch.object(bot, "append_technical_event"):
            result = bot.checked_preorder_quote("ZK-USDT", "LONG", "0.013372")
        self.assertLess(result["adverse_gap_pct"], 0.25)

    def test_stale_quote_fails_closed(self):
        quote = [{
            "instId": "ZK-USDT", "bidPrice": "0.013380",
            "askPrice": "0.013385", "ts": "1000",
        }]
        with patch.object(bot, "market_get", return_value=quote), \
             patch.object(bot, "now_ms", return_value=2000000), \
             patch.object(bot, "append_technical_event"):
            with self.assertRaisesRegex(RuntimeError, "stale"):
                bot.checked_preorder_quote("ZK-USDT", "LONG", "0.013372")


if __name__ == "__main__":
    unittest.main()
