"""Read-only tests for first-touch historical audit (no real exchange orders)."""
import unittest
from unittest.mock import patch

import blofin_live_scan_audit as audit


def minute(ts, o, h, l, c):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c}


class FirstTouchTests(unittest.TestCase):
    def test_long_tp_first(self):
        candles = [minute(0, 100, 100.7, 99.9, 100.4),
                   minute(60000, 100.4, 100.5, 99.0, 99.4)]
        result = audit.resolve_first_touch("LONG", 100, .5, candles)
        self.assertEqual(result["outcome"], "TP_HIT")
        self.assertEqual(result["first_touch_minute_ms"], 0)

    def test_short_sl_first(self):
        candles = [minute(0, 100, 100.8, 99.8, 100.7)]
        self.assertEqual(
            audit.resolve_first_touch("SHORT", 100, .5, candles)["outcome"],
            "SL_HIT",
        )

    def test_both_within_minute_remains_unknown(self):
        candles = [minute(0, 100, 101, 99, 100.2)]
        result = audit.resolve_first_touch("LONG", 100, .5, candles)
        self.assertEqual(result["status"], "BOTH_SAME_MINUTE_UNKNOWN")
        self.assertEqual(result["outcome"], "BOTH_ORDER_UNKNOWN")

    def test_open_price_resolves_same_minute(self):
        candles = [minute(0, 100.7, 101, 99, 100)]
        self.assertEqual(
            audit.resolve_first_touch("LONG", 100, .5, candles)["outcome"],
            "TP_HIT",
        )

    def test_no_touch(self):
        candles = [minute(0, 100, 100.1, 99.9, 100)]
        self.assertEqual(
            audit.resolve_first_touch("SHORT", 100, .5, candles)["outcome"],
            "NEITHER",
        )

    def test_exact_15_confirmed_minutes(self):
        start = 900000
        end = 1800000
        rows = [
            [str(ts), "100", "101", "99", "100", "42", "0", "0", "1"]
            for ts in range(start, end, 60000)
        ]
        with patch.object(audit.bot, "market_get", return_value=list(reversed(rows))) as mocked:
            received = audit.confirmed_minutes("BTC-USDT", start, end)
            self.assertEqual(len(received), 15)
            self.assertEqual(received[0]["ts"], start)
            self.assertEqual(received[-1]["ts"], end - 60000)
            self.assertEqual(mocked.call_args.args[0], "/api/v1/market/candles")
            self.assertEqual(mocked.call_args.args[1]["after"], str(end))

    def test_incomplete_minutes_do_not_resolve(self):
        start, end = 900000, 1800000
        rows = [
            [str(ts), "100", "101", "99", "100", "42", "0", "0", "1"]
            for ts in range(start, end - 60000, 60000)
        ]
        with patch.object(audit.bot, "market_get", return_value=rows):
            with self.assertRaises(ValueError):
                audit.confirmed_minutes("BTC-USDT", start, end)

    def test_nearest_volume_result_and_fees(self):
        hist = {"scans": [{
            "instruments": [{
                "inst": "Q-USDT", "rank": 1,
                "candle": {"color": "GREEN"},
                "long": {
                    "score": "6/7", "missing": ["STOCH"],
                    "volume_vs_last_red_pct": 20,
                },
                "next_candle_review": {"LONG": {"outcome_0_5": "TP_HIT"}},
            }, {
                "inst": "US-USDT", "rank": 2,
                "candle": {"color": "RED"},
                "short": {
                    "score": "10/10", "missing": [],
                    "volume_vs_last_green_pct": -10,
                },
                "next_candle_review": {"SHORT": {"outcome_0_5": "TP_HIT"}},
            }],
        }]}
        audit.nearest_volume_shadow(hist)
        self.assertEqual(hist["scans"][0]["nearest_volume_shadow"]["inst"], "Q-USDT")
        self.assertEqual(hist["scans"][0]["nearest_volume_shadow"]["status"], "TP")
        self.assertEqual(hist["nearest_volume_shadow_summary"]["net_compounded_min_pct"], 0.38)


if __name__ == "__main__":
    unittest.main()
