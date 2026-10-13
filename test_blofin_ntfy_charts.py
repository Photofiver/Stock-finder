"""Offline tests for BloFin live entry/exit PNG notifications via ntfy."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import blofin_live_hourly as bot


class NtfyImageNotificationTests(unittest.TestCase):
    def setUp(self):
        self.topic = patch.object(bot, "NTFY_TOPIC", "unit-test-not-a-real-topic")
        self.topic.start()
        self.addCleanup(self.topic.stop)

    def test_png_uploads_as_one_notification_with_text_and_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "entry.png"
            file.write_bytes(b"\\x89PNG\\r\\n\\x1a\\n" + b"x" * 300)
            with patch.object(bot.requests, "put") as put, patch.object(bot.requests, "post") as post:
                bot.notify("OPEN LONG TEST-USDT 1x", "BloFin LIVE SMA CROSS", attachment_path=str(file))
            put.assert_called_once()
            post.assert_not_called()
            args, kwargs = put.call_args
            self.assertTrue(args[0].endswith("/unit-test-not-a-real-topic"))
            self.assertEqual(kwargs["headers"]["Filename"], "entry.png")
            self.assertEqual(kwargs["headers"]["Message"], "OPEN LONG TEST-USDT 1x")
            self.assertEqual(kwargs["headers"]["Content-Type"], "image/png")

    def test_missing_chart_sends_existing_text_notification(self):
        with patch.object(bot.requests, "put") as put, patch.object(bot.requests, "post") as post:
            bot.notify("EXIT LOSS TEST-USDT", "BloFin LIVE RESULT", attachment_path="/nonexistent/exit.png")
        put.assert_not_called()
        post.assert_called_once()

    def test_attachment_failure_falls_back_to_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "exit.png"
            file.write_bytes(b"testPNG")
            with patch.object(bot.requests, "put", side_effect=ConnectionError("ntfy offline")) as put, patch.object(bot.requests, "post") as post:
                bot.notify("EXIT LOSS TEST-USDT", "BloFin LIVE RESULT", attachment_path=str(file))
            put.assert_called_once()
            post.assert_called_once()

    def test_no_topic_is_silent(self):
        with patch.object(bot, "NTFY_TOPIC", ""), patch.object(bot.requests, "put") as put, patch.object(bot.requests, "post") as post:
            bot.notify("OPEN LONG TEST-USDT", attachment_path="/nonexistent/chart.png")
        put.assert_not_called()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
