import json
import unittest
from unittest.mock import patch

import blofin_ws_close_trigger as trigger


def event(ts, confirm="1", inst="BTC-USDT", channel="candle5m"):
    return {"arg": {"channel": channel, "instId": inst},
            "data": [[str(ts), "1", "2", "0.5", "1.5", "1", "1", "1", confirm]]}


class WebSocketCloseTriggerTests(unittest.TestCase):
    def test_prepared_runner_waits_until_next_close(self):
        boundary = 1_800_000_000_000
        self.assertEqual(trigger.select_close_ms(boundary + 3 * 60_000),
                         (boundary + trigger.SIGNAL_MS, True))

    def test_late_runner_uses_current_closed_boundary(self):
        boundary = 1_800_000_000_000
        self.assertEqual(trigger.select_close_ms(boundary + 18_000),
                         (boundary, False))

    def test_pre_close_and_wrong_instrument_rejected(self):
        expected = 1_800_000_300_000
        opened = expected - trigger.SIGNAL_MS
        self.assertFalse(trigger.matches_confirmed_close(event(opened, "0"), expected))
        self.assertFalse(trigger.matches_confirmed_close(event(opened, inst="ETH-USDT"), expected))
        self.assertFalse(trigger.matches_confirmed_close(event(opened, channel="candle1m"), expected))
        self.assertFalse(trigger.matches_confirmed_close(event(opened-trigger.SIGNAL_MS), expected))

    def test_closed_candle_for_exact_timestamp_accepted(self):
        expected = 1_800_000_300_000
        self.assertTrue(trigger.matches_confirmed_close(event(expected-trigger.SIGNAL_MS), expected))
        self.assertTrue(trigger.matches_confirmed_close({
            "arg": {"channel": "candle5m", "instId": "BTC-USDT"},
            "data": [{"ts": str(expected-trigger.SIGNAL_MS), "confirm": "1"}],
        }, expected))

    def test_malformed_or_unrelated_message_is_ignored(self):
        target = 1_800_000_300_000
        self.assertFalse(trigger.matches_confirmed_close({}, target))
        self.assertFalse(trigger.matches_confirmed_close({"arg": None, "data": []}, target))
        self.assertFalse(trigger.matches_confirmed_close(
            {"arg": {"channel":"candle5m","instId":"BTC-USDT"},
             "data":[["bad", 2, 3, 4, 5, 6, 7, 8, "1"]]}, target))

    def test_post_close_does_not_open_socket(self):
        boundary = 1_800_000_000_000
        with patch.object(trigger, "utc_ms", return_value=boundary + 4_000):
            with patch.object(trigger.time, "sleep") as sleep:
                result = trigger.wait_for_close()
        self.assertEqual(result, boundary)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
