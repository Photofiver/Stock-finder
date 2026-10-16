"""Offline tests for TOP20 directional order-book confirmation and archive."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import blofin_order_book as ob


class BookBot:
    def __init__(self, bids, asks, age_ms=200):
        self._bids = bids
        self._asks = asks
        self._age_ms = age_ms
        self._now = 1792140000000
        self.calls = []

    def now_ms(self):
        return self._now

    def market_get(self, route, params):
        self.calls.append((route, params))
        return [{"ts": str(self._now - self._age_ms),
                 "bids": self._bids, "asks": self._asks}]


def book(bid_qty, ask_qty):
    bids = [[str(100 - i / 100), str(bid_qty), "1"] for i in range(20)]
    asks = [[str(100.01 + i / 100), str(ask_qty), "1"] for i in range(20)]
    return bids, asks


class OrderBookTests(unittest.TestCase):
    def test_long_buy_depth_confirms(self):
        bids, asks = book(3, 1)
        b = BookBot(bids, asks)
        event = ob.capture(b, "TEST-USDT", "LONG", 65, b.now_ms()-1000)
        self.assertTrue(event["direction_confirmed"])
        self.assertGreater(event["bid_pct"], 70)
        self.assertEqual(len(event["bid_levels"]), 20)
        self.assertEqual(len(event["ask_levels"]), 20)
        self.assertEqual(b.calls[0][1]["size"], "20")

    def test_short_sell_depth_confirms(self):
        bids, asks = book(1, 3)
        b = BookBot(bids, asks)
        event = ob.capture(b, "TEST-USDT", "SHORT", 60, b.now_ms()-1000)
        self.assertTrue(event["direction_confirmed"])
        self.assertGreater(event["ask_pct"], 70)

    def test_opposite_depth_blocks_both_directions(self):
        bids, asks = book(1, 3)
        bot = BookBot(bids, asks)
        self.assertFalse(ob.capture(bot, "T-USDT", "LONG", 55, 1)["direction_confirmed"])
        bids2, asks2 = book(3, 1)
        self.assertFalse(ob.capture(BookBot(bids2, asks2),
                                    "T-USDT", "SHORT", 55, 1)["direction_confirmed"])

    def test_balanced_depth_blocks_with_62_percent_threshold(self):
        bids, asks = book(1, 1)
        for side in ("LONG", "SHORT"):
            self.assertFalse(
                ob.capture(BookBot(bids, asks), "T-USDT", side, 55, 1)["direction_confirmed"]
            )

    def test_new_62_percent_threshold_blocks_60_but_allows_67(self):
        # At nearly equal prices, book(3, 2) yields about 60% on the bid side.
        bids, asks = book(3, 2)
        below = ob.capture(BookBot(bids, asks), "T-USDT", "LONG", 60, 1)
        self.assertLess(below["directional_pct"], 62)
        self.assertEqual(below["min_directional_pct"], 62.0)
        self.assertFalse(below["direction_confirmed"])
        bids, asks = book(2, 1)
        above = ob.capture(BookBot(bids, asks), "T-USDT", "LONG", 60, 1)
        self.assertGreater(above["directional_pct"], 62)
        self.assertTrue(above["direction_confirmed"])

    def test_stale_missing_and_broken_book_fail_closed(self):
        bids, asks = book(2, 1)
        for bad in (BookBot(bids, asks, age_ms=16000),
                    BookBot(bids[:2], asks),
                    BookBot(bids, [])):
            with self.assertRaises(RuntimeError):
                ob.capture(bad, "TEST-USDT", "LONG", 55, 1)

    def test_archives_pass_and_rejected_snapshot_with_all_levels(self):
        bids, asks = book(3, 1)
        snapshot = ob.capture(BookBot(bids, asks), "TEST-USDT", "LONG", 55, 1)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ob, "AUDIT_DIR", folder):
                path = ob.append_audit(snapshot, "DIRECTION_CONFIRMED")
                ob.append_audit(snapshot, "DIRECTION_REJECTED")
                self.assertTrue(os.path.exists(path))
                with open(path, encoding="utf-8") as f:
                    records = [json.loads(line) for line in f]
                self.assertEqual(len(records), 2)
                self.assertEqual(records[0]["decision"], "DIRECTION_CONFIRMED")
                self.assertEqual(records[1]["decision"], "DIRECTION_REJECTED")
                self.assertEqual(len(records[0]["bid_levels"]), 20)
                self.assertEqual(records[0]["inst"], "TEST-USDT")


if __name__ == "__main__":
    unittest.main()
