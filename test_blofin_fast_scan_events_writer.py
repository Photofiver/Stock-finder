"""Offline tests for tiny per-candle scan logs; no network / broker calls."""
import json
import tempfile
import unittest
from pathlib import Path
from blofin_fast_scan_events_writer import append_fast_event, safe_scan_event

class LogTests(unittest.TestCase):
    def sample(self):
        return {
            "last_scan_close_ms":1792299600000,
            "last_top200":["BTC-USDT","ETH-USDT"],
            "BLOFIN_API_KEY":"secret-do-not-log",
            "red_sma_last_scan":{
                "strategy":"RED_02_06_THEN_RED_04_08_SMA10_MINUS2_LONG_V1",
                "signal_close_ms":1792299600000,
                "top200_count":200,
                "scanned_confirmed":200,"matched":1,
                "status":"MATCHES_NO_TRADE",
                "candidates":[{"inst":"BTC-USDT","rank":1}],
            }
        }
    def test_deduplicates_and_keeps_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/"state.json"
            output=Path(td)/"events"
            source.write_text(json.dumps(self.sample()),encoding="utf-8")
            p1=append_fast_event(source,output)
            p2=append_fast_event(source,output)
            self.assertEqual(p1,p2)
            self.assertEqual(len(p1.read_text().splitlines()),1)
            self.assertNotIn("secret-do-not-log",p1.read_text())
            self.assertEqual(json.loads(p1.read_text())["matched"],1)
            self.assertEqual(json.loads(p1.read_text())["top200_at_scan"],["BTC-USDT","ETH-USDT"])
    def test_mismatched_state_does_not_log_old_scan(self):
        state=self.sample()
        state["last_scan_close_ms"]+=300000
        self.assertIsNone(safe_scan_event(state))
    def test_other_strategies_not_logged_as_red(self):
        state=self.sample()
        state["red_sma_last_scan"]["strategy"]="SMA10_SMA20_LONG_5m"
        self.assertIsNone(safe_scan_event(state))
    def test_missing_state_file_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(append_fast_event(Path(td)/"missing.json",Path(td)/"events"))

if __name__=="__main__":
    unittest.main()
