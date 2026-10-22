"""Offline safety tests for four confirmed 5m tunnel points."""
import unittest
from blofin_pivot_tunnel import FIVE, evaluate_tunnel, pivot_exit_transition, second_boundary_bounce, confirmed_pivots, TUNNEL_LOOKBACK_CANDLES

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
                ("upper_price", "101.05271", "LONG_TP_GAP_ABOVE_060"),
                ("lower_price", "100.34956", "LONG_SL_GAP_BELOW_010")]:
            with self.subTest(field=field):
                sig = self.signal()
                sig["volume_price_tunnel"][field] = value
                result = pivot_long_entry_filter(sig, {"expected_fill": "100.45"})
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], reason)

    def test_uses_expected_fill_not_signal_close(self):
        from blofin_volume_flip_both_live import pivot_long_entry_filter
        sig = self.signal()
        sig["close"] = "100.45"
        result = pivot_long_entry_filter(sig, {"expected_fill": "100.40"})
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "LONG_TP_GAP_ABOVE_060")

    def test_ma10_price_gate_removed_but_other_entry_filters_unchanged(self):
        from blofin_volume_flip_both_live import pivot_long_entry_filter
        for sma10 in ["100.00001", "101", None, "NaN", "Infinity", "0"]:
            with self.subTest(sma10=sma10):
                sig = self.signal()
                sig["sma10"] = sma10
                result = pivot_long_entry_filter(sig, {"expected_fill": "100.45"})
                self.assertTrue(result["allowed"], result)

    def test_missing_stale_and_nonfinite_data_fail_closed(self):
        from blofin_volume_flip_both_live import pivot_long_entry_filter
        for field, value in [
                ("indicator_signal_close_ms", 0),
                ("indicator_status", "UNAVAILABLE"),
                ("indicator_timeframe", "15m")]:
            with self.subTest(field=field, value=value):
                sig = self.signal()
                sig[field] = value
                self.assertFalse(pivot_long_entry_filter(
                    sig, {"expected_fill": "100.45"})["allowed"])
        for field, value in [
                ("upper_price", None), ("upper_price", "NaN"),
                ("upper_price", "Infinity"), ("upper_price", "0"),
                ("lower_price", None), ("lower_price", "NaN"),
                ("lower_price", "Infinity"), ("lower_price", "0")]:
            with self.subTest(field=field, value=value):
                sig = self.signal()
                sig["volume_price_tunnel"][field] = value
                self.assertFalse(pivot_long_entry_filter(
                    sig, {"expected_fill": "100.45"})["allowed"])
        for price in [None, "NaN", "Infinity", "0"]:
            with self.subTest(expected_fill=price):
                self.assertFalse(pivot_long_entry_filter(
                    self.signal(), {"expected_fill": price})["allowed"])
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

class TestSecondUpperBounce(unittest.TestCase):
    def test_two_confirmed_upper_touches_only(self):
        rows = {}
        for i in range(12):
            rows[i*FIVE] = (9.5, 9.8, 9.0, 9.5, 100)
        # Two upper pivots, each confirmed by its following closed bar.
        rows[3*FIVE] = (9.5, 10.1, 9.0, 9.5, 100)
        rows[4*FIVE] = (9.5, 9.8, 9.0, 9.5, 100)
        rows[7*FIVE] = (9.5, 10.2, 9.0, 9.5, 100)
        rows[8*FIVE] = (9.5, 9.8, 9.0, 9.5, 100)
        pos = {
            "side": "LONG", "opened_ms": 2*FIVE,
            "pivot_tunnel": {"upper_anchors": [
                {"open_ms": 0, "price": 10.0},
                {"open_ms": FIVE, "price": 10.0},
            ]},
            "second_bounce_upper_count": 0,
        }
        self.assertFalse(second_boundary_bounce(pos, rows, 5*FIVE))
        self.assertEqual(pos["second_bounce_upper_count"], 1)
        self.assertFalse(second_boundary_bounce(pos, rows, 5*FIVE))
        self.assertEqual(pos["second_bounce_upper_count"], 1)
        self.assertTrue(second_boundary_bounce(pos, rows, 9*FIVE))
        self.assertEqual(pos["second_bounce_upper_count"], 2)

class TestFortyEightBarTunnelLookback(unittest.TestCase):
    def test_only_last_forty_eight_closed_five_minute_bars(self):
        self.assertEqual(TUNNEL_LOOKBACK_CANDLES, 48)
        rows = {i*FIVE: (9.0, 10.0, 8.0, 9.0, 100)
                for i in range(70)}
        # Old pivot at 19 is outside the last 48 bars [22..69].
        rows[19*FIVE] = (9.0, 15.0, 5.0, 9.0, 100)
        # The pivot at 25 has its left, center and right within the window.
        rows[25*FIVE] = (9.0, 16.0, 6.0, 9.0, 100)
        highs, lows = confirmed_pivots(rows, 70*FIVE)
        self.assertEqual([p["open_ms"] for p in highs], [25*FIVE])
        self.assertEqual([p["open_ms"] for p in lows], [25*FIVE])
        tunnel = evaluate_tunnel(rows, 70*FIVE)
        self.assertFalse(tunnel["entry_allowed"])
        self.assertEqual(tunnel["lookback_candles"], 48)


class TestLivePivotTrailingStop(unittest.TestCase):
    """Broker amendments are verified and never loosen an existing LONG SL."""

    class FakeBot:
        MARGIN_MODE = "isolated"
        def __init__(self, live_sl="0.0947", delay_confirmation=False):
            self.live_sl = live_sl
            self.delay_confirmation = delay_confirmation
            self.pending_sl = None
            self.calls = []
            self.saved = 0

        def d(self, x):
            from decimal import Decimal
            return Decimal(str(x))

        def clean_decimal(self, x):
            return str(x)

        def save_state(self, state):
            self.saved += 1

        def private_request(self, method, path, params=None, body=None):
            self.calls.append((method, path, body))
            if path == "/api/v1/trade/order-tpsl-detail":
                return {"state":"live", "tpslId":"stop1", "tpTriggerPrice":"0",
                        "slTriggerPrice":self.live_sl,
                        "slTriggerPriceType":"last"}
            if path == "/api/v1/trade/amend-tpsl":
                assert method == "POST"
                assert body["tpslId"] == "stop1"
                assert body.get("newSlTriggerPrice") > self.d("0")
                assert "newTpTriggerPrice" not in body
                self.pending_sl = body["newSlTriggerPrice"]
                if not self.delay_confirmation:
                    self.live_sl = self.pending_sl
                return {"code":"0", "tpslId":"stop1"}
            raise AssertionError("Unexpected broker call "+path)

    def position(self):
        return {"inst":"TEST-USDT", "side":"LONG", "strategy":"VOLUME_FLIP_BOTH_5M_V1",
                "opened_ms":7*FIVE, "signal_close_ms":6*FIVE,
                "reference_entry":"0.0951", "sl":"0.0947", "tp":"0",
                "tp_sl_mode":"SECOND_BOUNCE_SL_ONLY", "tpsl_id":"stop1",
                "pivot_tunnel":{"lower_anchors":[
                    {"open_ms":0,"confirmed_close_ms":2*FIVE,"price":0.0946},
                    {"open_ms":6*FIVE,"confirmed_close_ms":8*FIVE,"price":0.0947},
                ]}}

    def test_long_sl_moves_up_with_frozen_rising_lower_line(self):
        from blofin_volume_flip_both_live import _trail_confirmed_pivot_long_sl
        bot = self.FakeBot()
        pos = self.position()
        state = {"positions":{"TEST-USDT":pos}}
        at = 12*FIVE
        rows = {at-FIVE: (0.095, 0.0957, 0.0948, 0.0955, 120)}
        result = _trail_confirmed_pivot_long_sl(bot,state,pos,
                 {"tickSize":"0.0001"},rows,at,{"last":"0.0955"})
        self.assertEqual(result["status"],"BROKER_VERIFIED")
        self.assertEqual(pos["sl"],"0.0948")
        self.assertEqual(bot.live_sl,"0.0948")
        self.assertTrue(pos["sl_trailing_active"])
        self.assertNotIn("tunnel_sl_pending",pos)
        self.assertEqual(sum(1 for m,p,b in bot.calls if m=="POST"),1)
        self.assertIsNone(_trail_confirmed_pivot_long_sl(bot,state,pos,
            {"tickSize":"0.0001"},rows,at,{"last":"0.0955"}))
        self.assertEqual(sum(1 for m,p,b in bot.calls if m=="POST"),1)

    def test_sl_can_lock_profit_above_entry(self):
        from blofin_volume_flip_both_live import _trail_confirmed_pivot_long_sl, _protection_prices
        bot = self.FakeBot()
        pos = self.position()
        state = {"positions":{"TEST-USDT":pos}}
        at = 36*FIVE
        rows = {at-FIVE: (0.0955, 0.0957, 0.0953, 0.0955, 120)}
        result = _trail_confirmed_pivot_long_sl(bot,state,pos,
                 {"tickSize":"0.0001"},rows,at,{"last":"0.0955"})
        self.assertEqual(result["status"],"BROKER_VERIFIED")
        self.assertGreater(bot.d(pos["sl"]),bot.d(pos["reference_entry"]))
        self.assertEqual(_protection_prices(bot,pos,{"tickSize":"0.0001"})[1],
                         bot.d(pos["sl"]))

    def test_pending_confirmation_keeps_old_broker_stop(self):
        from blofin_volume_flip_both_live import (_trail_confirmed_pivot_long_sl,
                                                    _reconcile_pending_tunnel_sl)
        bot = self.FakeBot(delay_confirmation=True)
        pos = self.position()
        state = {"positions":{"TEST-USDT":pos}}
        at = 12*FIVE
        rows = {at-FIVE: (0.095, 0.0957, 0.0948, 0.0955, 120)}
        result = _trail_confirmed_pivot_long_sl(bot,state,pos,
                 {"tickSize":"0.0001"},rows,at,{"last":"0.0955"})
        self.assertEqual(result["status"],"PENDING_VERIFICATION")
        self.assertEqual(pos["sl"],"0.0947")
        self.assertEqual(bot.live_sl,"0.0947")
        self.assertIn("tunnel_sl_pending",pos)
        bot.live_sl = bot.pending_sl
        self.assertTrue(_reconcile_pending_tunnel_sl(bot,state,pos))
        self.assertEqual(pos["sl"],"0.0948")
        self.assertNotIn("tunnel_sl_pending",pos)

    def test_stop_does_not_move_if_support_falls_or_crosses_price(self):
        from blofin_volume_flip_both_live import _trail_confirmed_pivot_long_sl
        for support_direction, last in [("falling","0.0955"),("rising","0.0947")]:
            with self.subTest(support=support_direction,price=last):
                bot=self.FakeBot()
                pos=self.position()
                if support_direction=="falling":
                    pos["pivot_tunnel"]["lower_anchors"][1]["price"]=0.0945
                state={"positions":{"TEST-USDT":pos}}
                at=12*FIVE
                rows={at-FIVE:(0.095,0.0957,0.0946,0.0955,120)}
                result=_trail_confirmed_pivot_long_sl(bot,state,pos,
                     {"tickSize":"0.0001"},rows,at,{"last":last})
                self.assertIsNone(result)
                self.assertEqual(pos["sl"],"0.0947")
                self.assertFalse(any(method=="POST" for method,_,_ in bot.calls))


if __name__=="__main__":
    unittest.main()
