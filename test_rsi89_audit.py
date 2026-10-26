"""Offline, broker-free tests for full TOP200 RSI89 scan audit/history."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import blofin_volume_flip_both_live as live
from blofin_fast_scan_events_writer import append_rsi89_event


class TestRsi89FullScanAudit(unittest.TestCase):
    def setUp(self):
        self.close_ms = 20 * live.FIVE
        self.bars = [
            {"ts": i * live.FIVE, "o": 100.0, "h": 101.0,
             "l": 99.0, "c": 100.0, "v": 10.0 + i}
            for i in range(20)
        ]
        self.scan = {"candidates": [], "entries": [], "rejections": []}

    def audit(self, before, current, coins=None):
        top200 = coins or [f"COIN{i}-USDT" for i in range(200)]
        data = {"rsi14_previous": before, "rsi14": current,
                "indicator_status": "OK", "macd_hist": 0.5, "stoch_k": 50}
        with patch.object(live, "_readonly_entry_indicators", return_value=data):
            return live._readonly_rsi89_top200_audit(
                top200, {top200[0]: self.bars}, self.close_ms,
                self.scan, set(), {})

    def test_all_200_including_missing_are_saved_and_ranked(self):
        result = self.audit(88.5, 88.95)
        self.assertEqual(result["top200_count"], 200)
        self.assertEqual(len(result["coins"]), 200)
        self.assertEqual(result["rsi_available_count"], 1)
        self.assertEqual(result["reason_counts"]["NO_CONFIRMED_5M_CANDLE"], 199)
        self.assertEqual(result["reason_counts"]["RSI89_NOT_REACHED"], 1)
        self.assertEqual(result["nearest_approaching_from_below"][0]["inst"], "COIN0-USDT")
        self.assertAlmostEqual(result["nearest_approaching_from_below"][0]["gap_to_89_points"], 0.05)

    def test_exact_upward_cross_only_and_no_broker_calls(self):
        result = self.audit(88.5, 89.0)
        self.assertEqual(result["reason_counts"]["RSI89_CROSS_NO_ENTRY"], 1)
        self.assertTrue(result["coins"][0]["rsi89_trigger"])
        result = self.audit(89.5, 89.1)
        self.assertEqual(result["reason_counts"]["RSI89_ALREADY_HIGH_NO_FRESH_CROSS"], 1)
        self.assertFalse(result["coins"][0]["rsi89_trigger"])

    def test_future_bar_not_used(self):
        bars = self.bars + [
            {"ts": self.close_ms, "o": 100, "h": 102,
             "l": 98, "c": 101, "v": 30}
        ]
        audit = live._readonly_rsi89_top200_audit(
            ["TEST-USDT"], {"TEST-USDT": bars}, self.close_ms,
            self.scan, set(), {})
        self.assertEqual(audit["coins"][0]["reason"], "NO_CONFIRMED_5M_CANDLE")

    def test_full_gzip_and_summary_deduplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "live.json"
            events_dir = Path(directory) / "events"
            audit = self.audit(88.0, 88.8)
            state = {
                "last_scan_close_ms": self.close_ms,
                "volume_long_last_scan": {
                    "signal_close_ms": self.close_ms,
                    "strategy": live.STRATEGY, "status": "NO_MATCH",
                    "scanned_confirmed": 1, "matched": 0,
                    "entries": [], "rejections": [],
                    "rsi89_audit": audit,
                },
            }
            state_file.write_text(json.dumps(state), encoding="utf-8")
            append_rsi89_event(state_file, events_dir)
            append_rsi89_event(state_file, events_dir)
            archive = next(events_dir.glob("rsi89_full_5m_*.jsonl.gz"))
            summary = next(events_dir.glob("rsi89_nearest_5m_*.jsonl"))
            with gzip.open(archive, "rt", encoding="utf-8") as fp:
                rows = [json.loads(line) for line in fp]
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]["audit"]["coins"]), 200)
            summary_rows = [json.loads(line) for line in summary.read_text().splitlines()]
            self.assertEqual(len(summary_rows), 1)
            self.assertEqual(summary_rows[0]["nearest_approaching_from_below"][0]["inst"],
                             "COIN0-USDT")


if __name__ == "__main__":
    unittest.main()
