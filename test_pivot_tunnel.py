"""Offline safety tests for four confirmed 5m tunnel points."""
import unittest
from blofin_pivot_tunnel import FIVE, evaluate_tunnel, pivot_exit_transition

RAW = [
    (11, 9), (12, 9.5), (15, 10), (12, 8), (11, 5),
    (12, 8), (14, 9), (11, 6), (10, 4), (11, 5),
]

def make_rows(reverse=False, last_close=8):
    data = {}
    for i,(high,low) in enumerate(RAW):
        if reverse:
            high,low = 20-low,20-high
        middle=(high+low)/2
        close=last_close if i == 9 else middle
        if reverse and i == 9: close=20-last_close
        data[i*FIVE]=(middle,high,low,close,100+i)
    return data

class TestTunnel(unittest.TestCase):
    def test_short_four_points_entry_on_confirmation_only(self):
        rows=make_rows()
        result=evaluate_tunnel(rows,10*FIVE)
        self.assertTrue(result["entry_allowed"], result)
        self.assertEqual(result["tunnel_direction"],"SHORT")
        self.assertEqual(result["fourth_confirmed_close_ms"],10*FIVE)
        self.assertFalse(evaluate_tunnel(rows,9*FIVE)["entry_allowed"])
        rows[10*FIVE]=(8,13,7,8,130)
        self.assertFalse(evaluate_tunnel(rows,11*FIVE)["entry_allowed"])
    def test_long_four_points(self):
        result=evaluate_tunnel(make_rows(reverse=True),10*FIVE)
        self.assertTrue(result["entry_allowed"],result)
        self.assertEqual(result["tunnel_direction"],"LONG")
    def test_close_outside_blocks(self):
        self.assertFalse(evaluate_tunnel(make_rows(last_close=2),10*FIVE)["entry_allowed"])
    def test_restart_requires_four_new_swings(self):
        self.assertFalse(evaluate_tunnel(make_rows(),10*FIVE,after_ms=3*FIVE)["entry_allowed"])
    def test_wick_does_not_exit_short(self):
        rows=make_rows()
        position=evaluate_tunnel(rows,10*FIVE)
        rows[10*FIVE]=(13,16,12,13,100)
        self.assertFalse(pivot_exit_transition(position,rows,11*FIVE,"SHORT"))
        rows[11*FIVE]=(13,16,12,14.1,100)
        self.assertTrue(pivot_exit_transition(position,rows,12*FIVE,"SHORT"))
    def test_wick_does_not_exit_long(self):
        rows=make_rows(reverse=True)
        position=evaluate_tunnel(rows,10*FIVE)
        rows[10*FIVE]=(8,12,5.0,8,100)
        self.assertFalse(pivot_exit_transition(position,rows,11*FIVE,"LONG"))
        rows[11*FIVE]=(8,12,5.0,5.9,100)
        self.assertTrue(pivot_exit_transition(position,rows,12*FIVE,"LONG"))


class TestSelectedLongEntryFilter(unittest.TestCase):
    def signal(self):
        return {"side": "LONG", "signal_close_ms": 300000,
                "indicator_signal_close_ms": 300000,
                "indicator_status": "OK", "indicator_timeframe": "5m",
                "sma10": "100", "volume_price_tunnel": {
                    "upper_price": "101.0527", "lower_price": "100.34955"}}

    def test_all_inclusive_boundaries_pass_without_moving_lines(self):
        import copy
        from blofin_volume_flip_both_live import pivot_long_entry_filter
        sig = self.signal()
        before = copy.deepcopy(sig)
        result = pivot_long_entry_filter(sig, {"expected_fill": "100.45"})
        self.assertTrue(result["allowed"], result)
        self.assertEqual(sig, before)

    def test_each_filter_rejects_independently(self):
        from blofin_volume_flip_both_live import pivot_long_entry_filter
        for field, value, reason in [
                ("sma10", "100.00001", "LONG_MA10_GAP_BELOW_045"),
                ("upper_price", "101.05271", "LONG_TP_GAP_ABOVE_060"),
                ("lower_price", "100.34956", "LONG_SL_GAP_BELOW_010")]:
            with self.subTest(field=field):
                sig = self.signal()
                target = sig if field == "sma10" else sig["volume_price_tunnel"]
                target[field] = value
                result = pivot_long_entry_filter(sig, {"expected_fill": "100.45"})
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], reason)

    def test_uses_expected_fill_not_signal_close(self):
        from blofin_volume_flip_both_live import pivot_long_entry_filter
        sig = self.signal()
        sig["close"] = "100.45"
        result = pivot_long_entry_filter(sig, {"expected_fill": "100.40"})
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "LONG_MA10_GAP_BELOW_045")

    def test_missing_stale_and_nonfinite_data_fail_closed(self):
        from blofin_volume_flip_both_live import pivot_long_entry_filter
        for field, value in [("sma10", None), ("sma10", "NaN"),
                             ("sma10", "Infinity"), ("sma10", "0"),
                             ("indicator_signal_close_ms", 0),
                             ("indicator_status", "UNAVAILABLE")]:
            with self.subTest(field=field, value=value):
                sig = self.signal()
                sig[field] = value
                self.assertFalse(pivot_long_entry_filter(
                    sig, {"expected_fill": "100.45"})["allowed"])
        self.assertFalse(pivot_long_entry_filter(self.signal(), {})["allowed"])

    def test_new_short_blocked_before_any_bot_call(self):
        from unittest.mock import Mock, patch
        import blofin_volume_flip_both_live as live
        bot = Mock()
        with patch.object(live, "PIVOT_ENABLED", True):
            sig = {"inst": "TEST-USDT", "side": "SHORT"}
            self.assertFalse(live._open_one(bot, {}, sig, {}))
        self.assertEqual(sig["preorder_rejection"], "PIVOT_LONG_ONLY")
        self.assertEqual(bot.mock_calls, [])

if __name__=="__main__":
    unittest.main()
