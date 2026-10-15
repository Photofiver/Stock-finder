"""Offline safeguards for segmented BloFin 5m audit journals."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LIVE_SIGNAL_MINUTES"] = "5"
os.environ["BLOFIN_LIVE_ENABLED"] = "false"

import blofin_live_scan_audit as audit


class LiveAuditRotationTests(unittest.TestCase):
    def test_new_journal_ignores_legacy_oversized_history(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            old = folder / "live_watch_history_5m.json"
            old.write_text("historical data - leave untouched", encoding="utf-8")
            with patch.object(audit, "HISTORY_DIR", folder):
                selected = audit.choose_history_file(5)
                self.assertEqual(selected.name, "live_watch_history_5m_part0002.json")
                with patch.object(audit, "HISTORY_FILE", selected):
                    history = audit.load_history()
                    self.assertEqual(history["scans"], [])
                    audit.save_json(selected, history)
                self.assertTrue(selected.exists())
                self.assertEqual(json.loads(selected.read_text(encoding="utf-8"))["scans"], [])
            self.assertEqual(old.read_text(encoding="utf-8"), "historical data - leave untouched")

    def test_rotate_before_github_size_limit(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            with patch.object(audit, "HISTORY_DIR", folder), patch.object(audit, "AUDIT_PART_MAX_BYTES", 100):
                first = audit.choose_history_file(5)
                self.assertEqual(first.name, "live_watch_history_5m_part0002.json")
                first.write_bytes(b"x" * 100)
                second = audit.choose_history_file(5)
                self.assertEqual(second.name, "live_watch_history_5m_part0003.json")
                second.write_bytes(b"y" * 40)
                self.assertEqual(audit.choose_history_file(5), second)
                second.write_bytes(b"y" * 100)
                self.assertEqual(audit.choose_history_file(5).name, "live_watch_history_5m_part0004.json")
                self.assertEqual(first.stat().st_size, 100)

    def test_other_intervals_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(audit, "HISTORY_DIR", Path(d)):
                self.assertEqual(audit.choose_history_file(15).name, "live_watch_history.json")
                self.assertEqual(audit.choose_history_file(10).name, "live_watch_history_10m.json")


if __name__ == "__main__":
    unittest.main()
