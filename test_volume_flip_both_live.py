"""Offline no-credentials regression tests for live 5m volume-flip LONG AND SHORT."""
import unittest
from decimal import Decimal
from unittest.mock import patch
import blofin_volume_flip_both_live as live
import blofin_live_hourly as hourly
import test_reverse_short_long_live as prior

F=live.FIVE
T=prior.T

def candle(ts,o=100.0,c=100.0,volume=100):
    return {"ts":ts,"o":o,"h":max(o,c)+0.2,"l":min(o,c)-0.2,"c":c,"v":volume}

def bars(side):
    candles=[candle(T-(50-i)*F) for i in range(50)]
    if side=="LONG":
        candles[-2]=candle(T-2*F,o=100.1,c=99.9,volume=60)
        candles[-1]=candle(T-F,o=100.0,c=100.1,volume=80)
    else:
        candles[-2]=candle(T-2*F,o=100.0,c=100.1,volume=60)
        candles[-1]=candle(T-F,o=100.1,c=100.0,volume=80)
    return candles


def trusted_volume_trend_for_existing_scan_tests(bars, close_ms, previous=None):
    """Existing tests isolate non-trend filters; trend has its own real tests."""
    last = bars[-1]
    side = "LONG" if last["c"] > last["o"] else "SHORT"
    return {
        "day_start_ms": live._volume_trend_midnight_ms(close_ms),
        "last_close_ms": close_ms,
        "trend": side,
        "peak_volume": float(last["v"]),
        "trend_started_close_ms": close_ms,
    }


REAL_VOLUME_PRICE_TUNNEL = live.volume_price_tunnel_from_bars


def trusted_tunnel_for_legacy_scan_tests(bars, close_ms, side):
    """Isolate previous volume/quote/rank tests from the new tunnel rule."""
    return {"status": "VALID", "reason": "PRICE_INSIDE_STRAIGHT_TUNNEL",
            "entry_allowed": True, "breakout_confirmed": False,
            "upper_price": 101.0, "lower_price": 99.0,
            "tunnel_direction": side, "colour_order_changed": False,
            "side": side}


class FakeBot(prior.FakeBot):
    def __init__(self):
        super().__init__()
        self.now=T+1000
        self.exchange_tpsl={}
        self.amend_calls=0
        self.amend_uncertain=False
        self.defer_amend=False
    def checked_preorder_quote(self, inst, side, close, enforce_adverse_gap=True):
        quote = super().checked_preorder_quote(inst, side, close,
                                              enforce_adverse_gap=enforce_adverse_gap)
        if side == "SHORT":
            quote.update(adverse_gap_pct=0.06, spread_pct=0.02)
        return quote

    def private_request(self,method,path,body=None,params=None):
        if path=="/api/v1/trade/order-tpsl":
            self.posted.append((method,path,body))
            tid="test-"+str(len(self.posted))
            self.exchange_tpsl[body["instId"]]={
                "state":"live","instId":body["instId"],"tpslId":tid,
                "tpTriggerPrice":body["tpTriggerPrice"],
                "slTriggerPrice":body["slTriggerPrice"],
                "tpTriggerPriceType":body["tpTriggerPriceType"],
                "slTriggerPriceType":body["slTriggerPriceType"]}
            return [{"code":"0","tpslId":tid}]
        if path=="/api/v1/trade/order-tpsl-detail":
            return dict(self.exchange_tpsl.get(params["instId"],{}))
        if path=="/api/v1/trade/amend-tpsl":
            self.amend_calls+=1
            self.posted.append((method,path,body))
            if not self.defer_amend:
                entry=self.exchange_tpsl[body["instId"]]
                entry["tpTriggerPriceType"]=body["newTpTriggerPriceType"]
                entry["slTriggerPriceType"]=body["newSlTriggerPriceType"]
            if self.amend_uncertain: raise RuntimeError("HTTP 429 uncertain")
            return [{"code":"0","tpslId":body["tpslId"]}]
        return super().private_request(method,path,body)
    def bar_close_ms(self,b):
        return int(b["ts"])+F

class Setups(unittest.TestCase):
    def setUp(self):
        self.bot=FakeBot()
    def test_long_signal_red_then_bigger_green(self):
        s=live.entry_signal(self.bot,bars("LONG"),T)
        self.assertEqual(s["side"],"LONG")
        self.assertEqual(s["signal_volume"],80)
        self.assertLess(s["volume_ratio"],3) # intentionally no old 3-4x rule
    def test_short_signal_green_then_bigger_red(self):
        s=live.entry_signal(self.bot,bars("SHORT"),T)
        self.assertEqual(s["side"],"SHORT")
        self.assertEqual(s["signal_volume"],80)
    def test_entry_snapshot_contains_readonly_indicators(self):
        # Retain the existing volume entry; indicators are observations only.
        for side in ("LONG", "SHORT"):
            signal = live.entry_signal(self.bot, bars(side), T)
            self.assertIsNotNone(signal)
            self.assertEqual(signal["indicator_status"], "OK",
                             signal.get("indicator_error"))
            self.assertEqual(signal["indicator_signal_close_ms"], T)
            for key in ("rsi14", "macd_dif", "macd_dea", "macd_hist",
                        "macd_hist_previous", "stoch_k", "stoch_d",
                        "sma10", "sma20", "atr14_pct", "obv_delta_5"):
                self.assertIsInstance(signal[key], (int, float), key)
            self.assertIsInstance(signal["sr_levels"], dict)
            self.assertIsInstance(signal["sr_entry_decision_readonly"], dict)

    def test_readonly_indicators_ignore_future_candle(self):
        current = bars("LONG")
        before = live.entry_signal(self.bot, current, T)
        current.append(candle(T, o=100.0, c=400.0, volume=1e8))
        after = live.entry_signal(self.bot, current, T)
        for key in ("rsi14", "macd_hist", "macd_hist_previous",
                    "stoch_k", "stoch_d", "sma10", "sma20", "atr14_pct",
                    "obv_delta_5", "sr_levels"):
            self.assertEqual(before[key], after[key], key)

    def test_short_indicator_history_does_not_block_valid_volume_entry(self):
        # 23 bars are enough for the volume rule, but not full MACD.
        short_history = bars("LONG")[-23:]
        signal = live.entry_signal(self.bot, short_history, T)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["side"], "LONG")
        self.assertEqual(signal["indicator_status"],
                         "INSUFFICIENT_CONFIRMED_HISTORY")
        self.assertIsNone(signal["macd_hist"])

    def test_volume_strength_beats_price_rank_for_long_and_short(self):
        weak = bars("LONG")
        weak[-1]["v"] = 70
        strong = bars("SHORT")
        strong[-1]["v"] = 96
        low = live.entry_signal(self.bot, weak, T)
        high = live.entry_signal(self.bot, strong, T)
        self.assertEqual(low["side"], "LONG")
        self.assertEqual(high["side"], "SHORT")
        self.assertLess(low["volume_strength_20"], high["volume_strength_20"])
        for signal, coin, rank in ((low, "LOWRANK-USDT", 1),
                                   (high, "HIGHRANK-USDT", 178)):
            signal.update(inst=coin, rank=rank)
            self.assertAlmostEqual(signal["volume_strength_20"],
                                   signal["signal_volume"]/signal["signal_mean20_prev"])
        selected = live.prioritize_volume_signals([low, high])
        self.assertEqual([s["inst"] for s in selected],
                         ["HIGHRANK-USDT", "LOWRANK-USDT"])
        self.assertEqual([s["rank"] for s in selected], [178, 1])
        self.assertEqual([s["side"] for s in selected], ["SHORT", "LONG"])

    def test_equal_volume_strength_has_stable_tie_break(self):
        a = {"inst":"COIN-A","rank":42,"volume_strength_20":0.8}
        b = {"inst":"COIN-B","rank":5,"volume_strength_20":0.8}
        self.assertEqual(live.prioritize_volume_signals([a,b]), [b,a])

    def test_extra_long_gates_keep_short_unchanged(self):
        long_signal = live.entry_signal(self.bot, bars("LONG"), T)
        self.assertLess(long_signal["signal_mean20_prev"],
                        long_signal["previous_mean20_prev"])
        self.assertIsNone(live.long_entry_extra_rejection(long_signal, 1))
        self.assertIsNone(live.long_entry_extra_rejection(long_signal, 50))
        self.assertIsNone(live.long_entry_extra_rejection(long_signal, 51))
        self.assertIsNone(live.long_entry_extra_rejection(long_signal, 200))
        self.assertEqual(live.long_entry_extra_rejection(long_signal, 201),
                         "LONG_RANK_OUTSIDE_TOP200")
        self.assertEqual(live.long_entry_extra_rejection(long_signal, 0),
                         "LONG_RANK_OUTSIDE_TOP200")
        long_signal["signal_mean20_prev"] = long_signal["previous_mean20_prev"]
        self.assertEqual(live.long_entry_extra_rejection(long_signal, 1),
                         "LONG_VOLUME_MA20_NOT_DECLINING")
        long_signal["signal_mean20_prev"] = None
        self.assertEqual(live.long_entry_extra_rejection(long_signal, 1),
                         "LONG_VOLUME_MA20_UNAVAILABLE")
        short_signal = live.entry_signal(self.bot, bars("SHORT"), T)
        short_signal["signal_mean20_prev"] = None
        self.assertIsNone(live.long_entry_extra_rejection(short_signal, 180))

    def test_short_quote_gates_fail_closed_only_for_short(self):
        good = {"adverse_gap_pct": 0.05, "spread_pct": 0.05}
        self.assertIsNone(live.short_entry_extra_rejection("SHORT", good))
        self.assertIsNone(live.short_entry_extra_rejection(
            "SHORT", {"adverse_gap_pct": 0.09, "spread_pct": 0.01}))
        self.assertEqual(
            live.short_entry_extra_rejection(
                "SHORT", {"adverse_gap_pct": 0.049, "spread_pct": 0.01}),
            "SHORT_ADVERSE_GAP_BELOW_005PCT")
        self.assertEqual(
            live.short_entry_extra_rejection(
                "SHORT", {"adverse_gap_pct": 0.08, "spread_pct": 0.051}),
            "SHORT_SPREAD_ABOVE_005PCT")
        self.assertEqual(live.short_entry_extra_rejection("SHORT", {}),
                         "SHORT_QUOTE_METRICS_UNAVAILABLE")
        self.assertEqual(live.short_entry_extra_rejection(
            "SHORT", {"adverse_gap_pct": float("nan"), "spread_pct": 0.01}),
            "SHORT_QUOTE_METRICS_UNAVAILABLE")
        self.assertIsNone(live.short_entry_extra_rejection("LONG", {}))

    def test_block_if_volume_above_mean20(self):
        b=bars("LONG");b[-1]["v"]=120
        self.assertIsNone(live.entry_signal(self.bot,b,T))
        b=bars("SHORT");b[-2]["v"]=110
        self.assertIsNone(live.entry_signal(self.bot,b,T))
    def test_block_if_not_bigger_volume(self):
        for side in ("LONG","SHORT"):
            b=bars(side);b[-1]["v"]=60
            self.assertIsNone(live.entry_signal(self.bot,b,T))
    def test_block_same_colour_and_unclosed(self):
        b=bars("LONG")
        b[-2]=candle(T-2*F,o=100,c=100.1,volume=60)
        self.assertIsNone(live.entry_signal(self.bot,b,T))
        self.assertIsNone(live.entry_signal(self.bot,bars("LONG")[:-1],T))
        self.assertIsNone(live.entry_signal(self.bot,bars("LONG"),T+F))
    def test_close_long_on_bigger_qualified_red(self):
        pos={"side":"LONG","signal_volume":60}
        b=bars("SHORT")
        self.assertEqual(live.exit_transition(pos,b,T),(True,60))
    def test_close_short_on_bigger_qualified_green(self):
        pos={"side":"SHORT","signal_volume":60}
        b=bars("LONG")
        self.assertEqual(live.exit_transition(pos,b,T),(True,60))
    def test_green_143_passes_MA20_but_would_fail_MA10(self):
        """XPT-like case: last red 110, current green 143, MA10 ~101, MA20 ~151."""
        b=bars("LONG")
        for candle20 in b[-21:-11]:
            candle20["v"]=200
        b[-2]["v"]=110
        b[-1]["v"]=143
        own=live._by_ts(b,T)
        self.assertLess(live._previous10_mean(own,T-F),143)
        self.assertGreaterEqual(live._previous20_mean(own,T-F),143)
        self.assertEqual(live.entry_signal(self.bot,b,T)["side"],"LONG")
        self.assertEqual(live.exit_transition({"side":"SHORT","signal_volume":110},b,T),(True,110))

    def test_MA20_rejects_candle_that_MA10_would_allow(self):
        b=bars("LONG")
        for recent in b[-11:-1]:
            recent["v"]=200
        b[-2]["v"]=100
        b[-1]["v"]=170
        own=live._by_ts(b,T)
        self.assertGreaterEqual(live._previous10_mean(own,T-F),170)
        self.assertLess(live._previous20_mean(own,T-F),170)
        self.assertIsNone(live.entry_signal(self.bot,b,T))
        self.assertEqual(live.exit_transition({"side":"SHORT","signal_volume":100},b,T),(True,100))

    def test_MA20_requires_full_previous_20_closed_candles(self):
        b=bars("SHORT")[-20:]
        self.assertIsNone(live.entry_signal(self.bot,b,T))
        self.assertEqual(live.exit_transition({"side":"LONG","signal_volume":50},b,T),(True,50))
        self.assertIsNotNone(live.entry_signal(self.bot,bars("SHORT"),T))

    def test_legacy_MA10_unchanged_on_existing_positions(self):
        b=bars("LONG")
        for candle20 in b[-21:-11]:
            candle20["v"]=200
        b[-1]["v"]=143
        old={"side":"LONG","last_red_volume":60}
        self.assertEqual(live.legacy_exit_transition(old,b,T),(False,60))

    def test_exit_has_no_MA20_filter_but_requires_bigger_volume(self):
        pos={"side":"LONG","signal_volume":80}
        b=bars("SHORT");self.assertEqual(live.exit_transition(pos,b,T),(False,80))
        b[-1]["v"]=120;self.assertEqual(live.exit_transition(pos,b,T),(True,80))
    def test_reference_stays_at_ENTRY_volume_even_after_new_same_side_candles(self):
        for side in ("LONG","SHORT"):
            pos={"side":side,"signal_volume":80}
            b=bars(side)
            self.assertEqual(live.exit_transition(pos,b,T),(False,80))
            b[-1]["v"]=60
            self.assertEqual(live.exit_transition(pos,b,T),(False,80))
    def test_XPT_short_closes_at_green143_above_entry_red116_even_above_MA20(self):
        b=bars("LONG")
        b[-1]["v"]=143
        pos={"side":"SHORT","signal_snapshot":{"signal_volume":116},"last_same_volume":110}
        own=live._by_ts(b,T)
        self.assertGreater(b[-1]["v"],live._previous20_mean(own,T-F))
        self.assertEqual(live.exit_transition(pos,b,T),(True,116))

    def test_opposite_colour_green_equal_to_entry_volume_does_not_close(self):
        b=bars("LONG")
        b[-1]["v"]=116
        self.assertEqual(live.exit_transition({"side":"SHORT","signal_volume":116},b,T),(False,116))
        b[-1]["v"]=117
        self.assertEqual(live.exit_transition({"side":"SHORT","signal_volume":116},b,T),(True,116))

    def test_missing_entry_volume_must_not_close_trade(self):
        self.assertEqual(live.exit_transition({"side":"SHORT"},bars("LONG"),T),(False,0))
        self.assertEqual(live.exit_transition({"side":"LONG","last_same_volume":12},bars("SHORT"),T),(False,0))

    def test_entry_uses_MA20_but_exit_ignores_MA20_for_LONG(self):
        b=bars("SHORT")
        b[-1]["v"]=180
        self.assertIsNone(live.entry_signal(self.bot,b,T))
        self.assertEqual(live.exit_transition({"side":"LONG","signal_volume":80},b,T),(True,80))

    def test_legacy_exit_preserved(self):
        old={"side":"LONG","last_red_volume":60}
        b=bars("LONG")
        self.assertEqual(live.legacy_exit_transition(old,b,T),(True,60))
    def test_no_future_candles(self):
        b=bars("SHORT")
        b.append(candle(T,o=1,c=5,volume=10000))
        self.assertEqual(live.entry_signal(self.bot,b,T),
                         live.entry_signal(self.bot,b[:-1],T))

class VolumePriceTunnel(unittest.TestCase):
    """The screenshot has two straight RED/GREEN price segments, not SMAs."""

    def sample(self, side, signal_close=None):
        rows = [candle(T-(55-i)*F, o=100, c=100, volume=50)
                for i in range(55)]
        if side == "LONG":
            # Earliest GREEN and latest GREEN define upper line HIGH->HIGH.
            rows[-18] = candle(T-18*F, o=101, c=102, volume=10)
            rows[-7] = candle(T-7*F, o=103, c=104, volume=20)
            # Earliest RED and latest RED define lower line LOW->LOW.
            rows[-16] = candle(T-16*F, o=99, c=98, volume=40)
            rows[-5] = candle(T-5*F, o=100, c=99, volume=30)
            rows[-2] = candle(T-2*F, o=103, c=102, volume=60)
            rows[-1] = candle(T-F, o=103,
                              c=103.5 if signal_close is None else signal_close,
                              volume=94)
        else:
            rows[-18] = candle(T-18*F, o=109, c=108, volume=10)
            rows[-7] = candle(T-7*F, o=108, c=107, volume=20)
            rows[-16] = candle(T-16*F, o=103, c=104, volume=40)
            rows[-5] = candle(T-5*F, o=101, c=102, volume=30)
            rows[-2] = candle(T-2*F, o=101, c=103, volume=60)
            rows[-1] = candle(T-F, o=103,
                              c=102 if signal_close is None else signal_close,
                              volume=94)
        return rows

    def test_long_green_upper_red_lower_straight_and_inside(self):
        channel = live.volume_price_tunnel_from_bars(self.sample("LONG"), T, "LONG")
        self.assertEqual(channel["status"], "VALID")
        self.assertTrue(channel["entry_allowed"])
        self.assertFalse(channel["breakout_confirmed"])
        self.assertEqual(channel["reason"], "PRICE_INSIDE_STRAIGHT_TUNNEL")
        self.assertEqual((channel["upper_color"], channel["lower_color"]),
                         ("GREEN", "RED"))
        self.assertEqual(
            [x["volume"] for x in channel["upper_anchors"]], [10, 20])
        self.assertEqual(
            [x["volume"] for x in channel["lower_anchors"]], [40, 60])
        self.assertAlmostEqual(
            channel["upper_price"] - channel["previous_upper_price"],
            channel["upper_slope_per_5m"])
        self.assertAlmostEqual(
            channel["lower_price"] - channel["previous_lower_price"],
            channel["lower_slope_per_5m"])
        self.assertLessEqual(channel["lower_price"], channel["signal_close_price"])
        self.assertLessEqual(channel["signal_close_price"], channel["upper_price"])

    def test_short_red_upper_green_lower_straight_and_inside(self):
        channel = live.volume_price_tunnel_from_bars(self.sample("SHORT"), T, "SHORT")
        self.assertEqual(channel["status"], "VALID")
        self.assertEqual((channel["upper_color"], channel["lower_color"]),
                         ("RED", "GREEN"))
        self.assertTrue(channel["entry_allowed"])
        self.assertEqual(channel["reason"], "PRICE_INSIDE_STRAIGHT_TUNNEL")
        self.assertLessEqual(channel["lower_price"], channel["signal_close_price"])
        self.assertLessEqual(channel["signal_close_price"], channel["upper_price"])

    def test_two_strongest_volume_anchors_per_colour_selected(self):
        # The highest-volume bars replace the older FIRST/LAST selection.
        rows=self.sample("LONG")
        rows[-12] = candle(T-12*F, o=102, c=103, volume=1000000)
        rows[-10] = candle(T-10*F, o=99, c=98, volume=500000)
        channel=live.volume_price_tunnel_from_bars(rows, T, "LONG")
        self.assertTrue(channel["entry_allowed"])
        self.assertEqual(channel["anchor_selection"],
                         "TOP_2_VOLUME_BARS_PER_COLOUR_CHRONOLOGICAL")
        self.assertEqual(
            [x["volume"] for x in channel["upper_anchors"]], [1000000,20])
        self.assertEqual(
            [x["volume"] for x in channel["lower_anchors"]], [500000,60])

    def test_inside_allowed_without_breakout_and_beyond_blocked(self):
        for side, beyond in (("LONG",110), ("SHORT",95)):
            inside=live.volume_price_tunnel_from_bars(self.sample(side), T, side)
            outside=live.volume_price_tunnel_from_bars(
                self.sample(side, signal_close=beyond), T, side)
            self.assertTrue(inside["entry_allowed"])
            self.assertFalse(outside["entry_allowed"])
            self.assertEqual(outside["reason"],
                             "PRICE_ABOVE_TUNNEL" if side=="LONG"
                             else "PRICE_BELOW_TUNNEL")

    def test_crossed_real_price_lines_are_direction_change_not_bad_data(self):
        rows=self.sample("SHORT")
        # GREEN latest low rises ABOVE the falling RED upper line.
        rows[-2] = candle(T-2*F, o=115, c=116, volume=60)
        signal=live.volume_price_tunnel_from_bars(rows, T, "SHORT")
        self.assertEqual(signal["status"], "VALID")
        self.assertTrue(signal["colour_order_changed"])
        self.assertEqual(signal["tunnel_direction"], "LONG")
        self.assertEqual(signal["reason"], "PRICE_LINES_CROSSED_TREND_REVERSED")
        self.assertFalse(signal["entry_allowed"])

    def test_missing_volume_anchor_or_closed_history_fails_safe(self):
        for side in ("LONG","SHORT"):
            self.assertEqual(
                live.volume_price_tunnel_from_bars(bars(side), T, side)["reason"],
                "INSUFFICIENT_GREEN_RED_ANCHORS")
            rows=self.sample(side)
            rows.pop(-9)
            result=live.volume_price_tunnel_from_bars(rows, T, side)
            self.assertEqual(result["status"], "UNAVAILABLE")
            self.assertEqual(result["reason"], "MISSING_CHANNEL_HISTORY")
            self.assertFalse(result["entry_allowed"])

    def test_future_candle_does_not_change_lines_or_inside_decision(self):
        rows=self.sample("LONG")
        before=live.volume_price_tunnel_from_bars(rows, T, "LONG")
        rows.append(candle(T, o=100, c=1000, volume=1000000))
        self.assertEqual(before,live.volume_price_tunnel_from_bars(rows,T,"LONG"))


class VolumeTrend(unittest.TestCase):
    def mkbars(self, volumes_and_colours, start=None):
        start = start if start is not None else live._volume_trend_midnight_ms(T)
        rows = []
        for i, (volume, colour) in enumerate(volumes_and_colours):
            o = 100.0
            c = 101.0 if colour == "GREEN" else 99.0 if colour == "RED" else o
            rows.append(candle(start+i*F, o=o, c=c, volume=volume))
        return rows, start

    def test_day_peak_trend_changes_only_on_strict_opposite_volume(self):
        sample, start = self.mkbars([
            (100, "GREEN"), (80, "RED"), (120, "GREEN"),
            (120, "RED"), (121, "RED"), (119, "GREEN"),
            (122, "GREEN")])
        expect = ["LONG", "LONG", "LONG", "LONG", "SHORT", "SHORT", "LONG"]
        saved = None
        for i, direction in enumerate(expect):
            end = start+(i+1)*F
            # Reuse full available candles; incrementally update live state.
            saved = live.volume_trend_from_bars(sample, end, saved)
            self.assertIsNotNone(saved)
            self.assertEqual(saved["trend"], direction)
        self.assertEqual(saved["peak_volume"], 122)
        self.assertEqual(saved["day_start_ms"], start)

    def test_missing_candle_is_not_assumed_and_cannot_trade(self):
        rows, start = self.mkbars([
            (100, "GREEN"), (50, "RED"), (90, "RED")])
        self.assertEqual(
            live.volume_trend_from_bars(rows, start+2*F)["trend"], "LONG")
        with_gap = [rows[0], rows[2]]
        self.assertIsNone(live.volume_trend_from_bars(
            with_gap, start+3*F))
        prev = live.volume_trend_from_bars(rows, start+F)
        self.assertIsNone(live.volume_trend_from_bars(
            with_gap, start+3*F, prev))

    def test_new_day_resets_peak_and_future_candles_ignored(self):
        start = live._volume_trend_midnight_ms(T)
        first, _ = self.mkbars([(600, "RED"), (100, "GREEN")], start)
        snap = live.volume_trend_from_bars(first, start+2*F)
        self.assertEqual(snap["trend"], "SHORT")
        next_day = live._volume_trend_midnight_ms(start+25*60*60*1000)
        self.assertGreater(next_day, start)
        next_bars, _ = self.mkbars([(100, "GREEN")], next_day)
        reset = live.volume_trend_from_bars(next_bars, next_day+F, snap)
        self.assertEqual(reset["trend"], "LONG")
        self.assertEqual(reset["peak_volume"], 100.0)
        # Later unfinished/future bar must not change the historical signal.
        rows = first + [candle(start+2*F, o=100, c=99, volume=900)]
        self.assertEqual(
            live.volume_trend_from_bars(rows, start+2*F)["trend"], "SHORT")

    def test_no_opening_bar_today_returns_unknown(self):
        midnight = live._volume_trend_midnight_ms(T)
        rows, _ = self.mkbars([(500, "GREEN")], midnight+F)
        self.assertIsNone(live.volume_trend_from_bars(rows, midnight+2*F))


class Broker(unittest.TestCase):
    def setUp(self):
        self.bot=FakeBot()
        self.state={"positions":{}}
        # Other LIVE tests cover order and volume gates without dependence on
        # UK's midnight time; dedicated VolumeTrend tests use real logic.
        stub = patch.object(
            live, "volume_trend_from_bars",
            side_effect=trusted_volume_trend_for_existing_scan_tests)
        stub.start()
        self.addCleanup(stub.stop)
        tunnel_stub = patch.object(
            live, "volume_price_tunnel_from_bars",
            side_effect=trusted_tunnel_for_legacy_scan_tests)
        tunnel_stub.start()
        self.addCleanup(tunnel_stub.stop)
        self.instruments={"ABC-USDT":{"contractValue":"1","tickSize":"0.01"},
                          "XYZ-USDT":{"contractValue":"1","tickSize":"0.01"}}
    def signal(self,coin,side):
        s=live.entry_signal(self.bot,bars(side),T)
        s.update(inst=coin,rank=1)
        s["volume_price_tunnel"] = trusted_tunnel_for_legacy_scan_tests(
            bars(side), T, side)
        return s
    def test_live_trend_rejects_opposite_entry_and_logs_reason(self):
        ranking=["LONG-USDT","SHORT-USDT"]+[f"OTHER-{i}" for i in range(198)]
        observed=[]
        def confirmed(insts, close):
            up = bars("LONG")
            down = bars("SHORT")
            up[-1]["v"] = 94
            down[-1]["v"] = 94
            return {"LONG-USDT": up, "SHORT-USDT": down}
        def opposite_trend(history, close_ms, previous=None):
            t = trusted_volume_trend_for_existing_scan_tests(history, close_ms)
            t["trend"] = "SHORT" if t["trend"] == "LONG" else "LONG"
            return t
        def observe(bot, state, sig, meta):
            observed.append(sig["inst"])
            return False
        with patch.dict("os.environ", {"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES": "false"}):
            with patch.object(live, "volume_trend_from_bars", side_effect=opposite_trend):
                with patch.object(live, "_open_one", side_effect=observe):
                    live.run(self.bot, self.state, ranking, {}, self.instruments, T, confirmed)
        self.assertEqual(observed, [])
        reasons = self.state["volume_long_last_scan"]["rejections"]
        self.assertEqual(
            [(r["inst"], r["reason"]) for r in reasons if r["reason"]=="VOLUME_TREND_OPPOSES_ENTRY"],
            [("LONG-USDT", "VOLUME_TREND_OPPOSES_ENTRY"),
             ("SHORT-USDT", "VOLUME_TREND_OPPOSES_ENTRY")])

    def test_new_volume_tunnel_blocks_entries_without_valid_anchors(self):
        ranking = ["ABC-USDT", "XYZ-USDT"] + [
            f"OTHER-{i}-USDT" for i in range(198)]
        up = bars("LONG")
        down = bars("SHORT")
        up[-1]["v"] = 94
        down[-1]["v"] = 94
        def confirmed(insts, close):
            return {"ABC-USDT": up, "XYZ-USDT": down}
        with patch.dict("os.environ", {"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES": "false"}):
            with patch.object(live, "volume_price_tunnel_from_bars",
                              side_effect=REAL_VOLUME_PRICE_TUNNEL):
                with patch.object(live, "_open_one") as open_order:
                    live.run(self.bot, self.state, ranking, {}, self.instruments,
                             T, confirmed)
                    open_order.assert_not_called()
        rejections = self.state["volume_long_last_scan"]["rejections"]
        blocked = [x for x in rejections
                   if x["reason"] == "VOLUME_TUNNEL_UNAVAILABLE"]
        self.assertEqual({x["inst"] for x in blocked},
                         {"ABC-USDT", "XYZ-USDT"})
        self.assertEqual(self.state["volume_long_last_scan"]["matched"], 0)

    def test_missing_tunnel_direction_is_rejected_without_exception(self):
        ranking = ["ABC-USDT"] + [f"OTHER-{i}" for i in range(199)]
        sample = bars("LONG")
        sample[-1]["v"] = 94
        def confirmed(insts, close):
            return {"ABC-USDT": sample}
        incomplete = {"status": "VALID", "entry_allowed": True,
                      "reason": "MISSING_TUNNEL_DIRECTION"}
        with patch.dict("os.environ", {"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES": "false"}):
            with patch.object(live, "volume_price_tunnel_from_bars",
                              return_value=incomplete):
                with patch.object(live, "_open_one") as open_order:
                    live.run(self.bot, self.state, ranking, {}, self.instruments, T, confirmed)
                    open_order.assert_not_called()
        rejected = self.state["volume_long_last_scan"]["rejections"]
        self.assertIn(("ABC-USDT", "VOLUME_TUNNEL_UNAVAILABLE"),
                      [(row["inst"], row["reason"]) for row in rejected])

    def test_full_top200_scan_attempts_strongest_volume_first(self):
        weak = bars("LONG")
        weak[-1]["v"] = 92
        strong = bars("SHORT")
        strong[-1]["v"] = 96
        ranking = ["ABC-USDT", "XYZ-USDT"] + [f"OTHER-{i}-USDT" for i in range(198)]
        seen = []
        def confirmed(insts, close):
            self.assertEqual(close, T)
            return {"ABC-USDT":weak, "XYZ-USDT":strong}
        def observe(bot, state, sig, instruments):
            seen.append((sig["inst"], sig["rank"], sig["volume_strength_20"]))
            return False
        with patch.dict("os.environ", {"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES":"false"}):
            with patch.object(live, "_open_one", side_effect=observe):
                live.run(self.bot, self.state, ranking, {}, self.instruments, T, confirmed)
        self.assertEqual([coin for coin, rank, strength in seen],
                         ["XYZ-USDT", "ABC-USDT"])
        self.assertEqual([rank for coin, rank, strength in seen], [2, 1])
        scan = self.state["volume_long_last_scan"]
        self.assertEqual(scan["matched"], 2)
        self.assertEqual(scan["selection_priority"], "SIGNAL_VOLUME_DIV_PREVIOUS_20_BAR_MEAN_DESC")
        self.assertEqual([x["inst"] for x in scan["candidates"]],
                         ["XYZ-USDT", "ABC-USDT"])
        self.assertEqual(scan["entries"], [])

    def test_removed_90_to_100_percent_gate_keeps_ma20_ceiling(self):
        # Prior MA20 = 98; no minimum 90% gate remains, but original
        # per-candle <= MA20 qualification stays in force.
        scenarios = (
            ("LOW-USDT", 88, "LONG"),       # 88/98 < 90%: now accepted
            ("MIN-USDT", 88.2, "SHORT"),     # exactly 90%: accepted
            ("MID-USDT", 94, "LONG"),       # between 90% and 100%: accepted
            ("UPPER-USDT", 98, "SHORT"),     # equal to MA20: accepted
            ("ABOVE-USDT", 101, "LONG"),    # greater than MA20: still rejected
        )
        ranking = [name for name,_,_ in scenarios] + [f"OTHER-{i}-USDT" for i in range(195)]
        observed = []
        def confirmed(insts, close_ms):
            self.assertEqual(close_ms, T)
            candles = {}
            for inst,vol,side in scenarios:
                candles[inst] = bars(side)
                candles[inst][-1]["v"] = vol
            return candles
        def observe(bot, state, sig, instruments):
            observed.append((sig["inst"], sig["volume_strength_20"]))
            return False
        with patch.dict("os.environ", {"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES":"false"}):
            with patch.object(live, "_open_one", side_effect=observe):
                live.run(self.bot,self.state,ranking,{},self.instruments,T,confirmed)
        self.assertEqual([x[0] for x in observed],
                         ["UPPER-USDT", "MID-USDT", "MIN-USDT", "LOW-USDT"])
        self.assertLess(observed[-1][1], .90)
        self.assertEqual(observed[0][1], 1.0)
        scan=self.state["volume_long_last_scan"]
        self.assertEqual(scan["matched"],4)
        self.assertEqual(scan["volume_entry_strength_range_filter"], "DISABLED")
        self.assertEqual([x["inst"] for x in scan["candidates"]],
                         ["UPPER-USDT", "MID-USDT", "MIN-USDT", "LOW-USDT"])

    def test_long_only_entry_gates_filter_rank_and_nonfalling_volume_mean(self):
        ranking = ["GOOD-LONG", "RISING-LONG", "GOOD-SHORT"] + [
            f"OTHER-{i}" for i in range(47)] + ["RANK51-LONG"] + [
            f"OTHER-LATE-{i}" for i in range(149)]
        self.assertEqual(len(ranking), 200)
        def confirmed(insts, close):
            self.assertEqual(close, T)
            rising = bars("LONG")
            # Make the current 20-bar mean rise compared to the preceding
            # average, without altering either of the signal candles.
            rising[-22]["v"] = 1
            rising[-1]["v"] = 94
            self.assertGreaterEqual(
                live.entry_signal(self.bot, rising, T)["volume_strength_20"], 0.90)
            good = bars("LONG")
            good[-1]["v"] = 94
            good_short = bars("SHORT")
            good_short[-1]["v"] = 94
            return {
                "GOOD-LONG": good,
                "RISING-LONG": rising,
                "GOOD-SHORT": good_short,
                "RANK51-LONG": good,
            }
        observed = []
        def observe(bot, state, sig, instruments):
            observed.append((sig["inst"], sig["side"], sig["rank"]))
            return False
        with patch.dict("os.environ", {"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES": "false"}):
            with patch.object(live, "_open_one", side_effect=observe):
                live.run(self.bot, self.state, ranking, {}, self.instruments,
                         T, confirmed)
        self.assertEqual(observed, [
            ("GOOD-LONG", "LONG", 1), ("GOOD-SHORT", "SHORT", 3),
            ("RANK51-LONG", "LONG", 51)])
        scan = self.state["volume_long_last_scan"]
        self.assertEqual(scan["long_entry_rank_range_inclusive"], [1, 200])
        self.assertTrue(scan["long_requires_falling_prior_20bar_mean"])
        reasons = {x["inst"]: x["reason"] for x in scan["rejections"]}
        self.assertEqual(reasons["RISING-LONG"], "LONG_VOLUME_MA20_NOT_DECLINING")
        self.assertNotIn("RANK51-LONG", reasons)

    def test_pivot_long_tp_distance_still_blocks_before_order(self):
        sig = self.signal("ABC-USDT", "LONG")
        sig["pivot_tunnel_entry"] = True
        sig["sma10"] = 100
        with patch.object(live, "PIVOT_ENABLED", True):
            self.assertFalse(live._open_one(self.bot, self.state, sig, self.instruments))
        self.assertEqual(sig["preorder_rejection"], "LONG_TP_GAP_ABOVE_060")
        self.assertEqual(self.bot.posted, [])
        self.assertEqual(self.state["positions"], {})
        self.assertNotIn("volume_long_order_attempts", self.state)

    @patch("blofin_trade_charts.capture_chart", side_effect=RuntimeError("offline"))
    def test_pivot_long_filter_pass_preserves_line_protection_and_capital(self, chart):
        sig = self.signal("ABC-USDT", "LONG")
        sig["pivot_tunnel_entry"] = True
        sig["sma10"] = 101  # Price is BELOW MA10; this must no longer block.
        sig["indicator_status"] = "INSUFFICIENT_CONFIRMED_HISTORY"  # Read-only.
        sig["volume_price_tunnel"].update(upper_price=100.5, lower_price=99.5)
        with patch.object(live, "PIVOT_ENABLED", True):
            self.assertTrue(live._open_one(self.bot, self.state, sig, self.instruments))
        pos = self.state["positions"]["ABC-USDT"]
        self.assertTrue(pos["signal_snapshot"]["pivot_long_entry_filter"]["allowed"])
        self.assertEqual((Decimal(pos["tp"]), Decimal(pos["sl"])),
                         (Decimal("100.5"), Decimal("99.5")))
        self.assertLessEqual(Decimal(pos["notional_usdt"]), Decimal("5"))
        self.assertEqual(self.bot.posted[0][2]["side"], "buy")

    def test_short_quote_filters_block_before_any_order_post(self):
        sig = self.signal("XYZ-USDT", "SHORT")
        for gap, spread, reason in [
            (0.049, 0.01, "SHORT_ADVERSE_GAP_BELOW_005PCT"),
            (0.08, 0.051, "SHORT_SPREAD_ABOVE_005PCT"),
        ]:
            sig.pop("preorder_rejection", None)
            sig.pop("pre_order_quote", None)
            with patch.object(self.bot, "checked_preorder_quote",
                return_value={"expected_fill":"100", "adverse_gap_pct":gap,
                              "spread_pct":spread}):
                self.assertFalse(live._open_one(
                    self.bot, self.state, sig, self.instruments))
            self.assertEqual(sig["preorder_rejection"], reason)
            self.assertFalse(self.state["positions"])
            self.assertEqual(self.bot.posted, [])
            self.assertNotIn("XYZ-USDT", self.state.get("volume_long_order_attempts", {}))

    @patch("blofin_trade_charts.capture_chart",side_effect=RuntimeError("no rendering in offline tests"))
    def test_buy_long_sell_short_each_one_per_coin_tpsl(self,chart):
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("ABC-USDT","LONG"),self.instruments))
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("XYZ-USDT","SHORT"),self.instruments))
        # Test account uses 20 USDT in total; at most 5 USDT per trade.
        self.assertEqual(self.state["positions"]["ABC-USDT"]["notional_usdt"],"5.00")
        # Remaining available capital is capped by the shared bankroll.
        self.assertEqual(self.state["positions"]["XYZ-USDT"]["notional_usdt"],"5.00")
        self.assertEqual(self.state["positions"]["ABC-USDT"]["side"],"LONG")
        self.assertEqual(self.state["positions"]["XYZ-USDT"]["side"],"SHORT")
        self.assertEqual(self.bot.posted[0][2]["side"],"buy")
        self.assertEqual(self.bot.posted[2][2]["side"],"sell")
        self.assertEqual(len(self.bot.posted),4)
        self.assertEqual([x[1] for x in self.bot.posted],
                         ["/api/v1/trade/order","/api/v1/trade/order-tpsl",
                          "/api/v1/trade/order","/api/v1/trade/order-tpsl"])
        a=self.state["positions"]["ABC-USDT"]
        b=self.state["positions"]["XYZ-USDT"]
        self.assertEqual((a["tp"],a["sl"]),("101.0","99.0"))
        self.assertEqual((b["tp"],b["sl"]),("99.0","101.0"))
        self.assertEqual((a["tp_policy"],b["sl_policy"]),("TUNNEL_LINE","TUNNEL_LINE"))
        self.assertEqual(a["tp_sl_mode"], live.TUNNEL_TPSL_MODE)
        self.assertEqual(b["tp_sl_mode"], live.TUNNEL_TPSL_MODE)
        self.assertEqual(self.bot.posted[1][2]["side"],"sell")
        self.assertEqual(self.bot.posted[3][2]["side"],"buy")
        self.assertFalse(live._open_one(self.bot,self.state,self.signal("ABC-USDT","LONG"),self.instruments))
        self.assertLessEqual(sum(Decimal(p["notional_usdt"]) for p in self.state["positions"].values()),Decimal("20"))
    def test_tunnel_line_prices_for_new_long_and_short_round_to_ticks(self):
        # No artificial distance or reward/risk restriction; prices are
        # the saved straight-line boundaries at the signal close.
        long_pos={"side":"LONG", "reference_entry":"0.04412",
                  "tp_sl_mode":live.TUNNEL_TPSL_MODE,
                  "tunnel_tp_line":"0.04761", "tunnel_sl_line":"0.0441146666666"}
        short_pos={"side":"SHORT", "reference_entry":"0.04412",
                   "tp_sl_mode":live.TUNNEL_TPSL_MODE,
                   "tunnel_tp_line":"0.043901", "tunnel_sl_line":"0.045002"}
        meta={"tickSize":"0.00001"}
        self.assertEqual(tuple(map(str,live._protection_prices(self.bot,long_pos,meta))),
                         ("0.04761","0.04411"))
        self.assertEqual(tuple(map(str,live._protection_prices(self.bot,short_pos,meta))),
                         ("0.04390","0.04501"))

    def test_invalid_or_missing_tunnel_targets_block_before_order(self):
        for invalid in (
            {"status":"VALID","tunnel_direction":"LONG","entry_allowed":True,
             "upper_price":100.0,"lower_price":99.0},
            {"status":"VALID","tunnel_direction":"LONG","entry_allowed":True,
             "upper_price":101.0,"lower_price":100.0},
            {"status":"VALID","tunnel_direction":"LONG","entry_allowed":True},
        ):
            sig=self.signal("ABC-USDT","LONG")
            sig["volume_price_tunnel"]=invalid
            self.assertFalse(live._open_one(self.bot,self.state,sig,self.instruments))
            self.assertIn(sig["preorder_rejection"],
                          ("TUNNEL_TPSL_UNTRADEABLE", "TUNNEL_TPSL_NOT_VALID"))
            self.assertEqual(self.bot.posted, [])
            self.assertFalse(self.state["positions"])

    def test_existing_position_protected_once(self):
        pos={"inst":"ABC-USDT","side":"LONG","strategy":live.STRATEGY,
             "reference_entry":"100","tp":"","sl":""}
        self.state["positions"]["ABC-USDT"]=pos
        self.assertTrue(live._ensure_protection(self.bot,self.state,pos,
                                                 self.instruments["ABC-USDT"]))
        self.assertTrue(live._ensure_protection(self.bot,self.state,pos,
                                                 self.instruments["ABC-USDT"]))
        self.assertEqual(len(self.bot.posted),1)
        self.assertEqual(pos["protection_status"],"TP055_SL05_MARK_ACTIVE")


    def test_tunnel_tpsl_uses_last_trade_price_both_directions(self):
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("ABC-USDT","LONG"),self.instruments))
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("XYZ-USDT","SHORT"),self.instruments))
        for coin in ("ABC-USDT","XYZ-USDT"):
            record=self.bot.exchange_tpsl[coin]
            self.assertEqual(record["tpTriggerPriceType"],"last")
            self.assertEqual(record["slTriggerPriceType"],"last")
            self.assertEqual(self.state["positions"][coin]["protection_status"],"TUNNEL_LINES_LAST_ACTIVE")

    def test_old_mark_tunnel_amended_to_last_without_moving_boundaries(self):
        pos={"strategy":live.STRATEGY,"inst":"ABC-USDT","side":"LONG",
             "reference_entry":"100","tp":"101.0","sl":"99.0",
             "tp_sl_mode":live.TUNNEL_TPSL_MODE,
             "tunnel_tp_line":"101.0","tunnel_sl_line":"99.0",
             "tpsl_id":"old-mark"}
        self.state["positions"]["ABC-USDT"]=pos
        self.bot.exchange_tpsl["ABC-USDT"]={
            "state":"live","tpslId":"old-mark",
            "tpTriggerPrice":"101.0","slTriggerPrice":"99.0",
            "tpTriggerPriceType":"mark","slTriggerPriceType":"mark"}
        self.assertTrue(live._ensure_protection(
            self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertEqual(self.bot.amend_calls,1)
        self.assertEqual(len(self.bot.posted),1)
        self.assertEqual(self.bot.posted[0][1],"/api/v1/trade/amend-tpsl")
        self.assertEqual(self.bot.posted[0][2]["newTpTriggerPriceType"],"last")
        self.assertEqual(self.bot.posted[0][2]["newSlTriggerPriceType"],"last")
        self.assertEqual(pos["tp"],"101.0")
        self.assertEqual(pos["sl"],"99.0")
        self.assertEqual(pos["tpsl_id"],"old-mark")
        self.assertEqual(pos["protection_status"],"TUNNEL_LINES_LAST_ACTIVE")

    def test_uncertain_mark_to_last_preserves_live_tunnel_order(self):
        pos={"strategy":live.STRATEGY,"inst":"ABC-USDT","side":"LONG",
             "reference_entry":"100","tp":"101.0","sl":"99.0",
             "tp_sl_mode":live.TUNNEL_TPSL_MODE,
             "tunnel_tp_line":"101.0","tunnel_sl_line":"99.0",
             "tpsl_id":"old-mark"}
        self.state["positions"]["ABC-USDT"]=pos
        self.bot.exchange_tpsl["ABC-USDT"]={
            "state":"live","tpslId":"old-mark",
            "tpTriggerPrice":"101.0","slTriggerPrice":"99.0",
            "tpTriggerPriceType":"mark","slTriggerPriceType":"mark"}
        self.bot.defer_amend=True
        self.bot.amend_uncertain=True
        self.assertTrue(live._ensure_protection(
            self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertTrue(pos["trigger_migration_deferred"])
        self.assertEqual(pos["protection_status"],
                         "TUNNEL_MARK_STILL_ACTIVE_AMEND_UNVERIFIED")
        self.assertEqual(self.bot.amend_calls,1)
        self.assertTrue(live._ensure_protection(
            self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertEqual(self.bot.amend_calls,1)  # never repeat ambiguous POST
        self.bot.exchange_tpsl["ABC-USDT"].update(
            tpTriggerPriceType="last",slTriggerPriceType="last")
        self.assertTrue(live._ensure_protection(
            self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertFalse(pos.get("trigger_migration_deferred",False))
        self.assertEqual(pos["protection_status"],"TUNNEL_LINES_LAST_ACTIVE")

    def test_existing_live_last_order_amended_without_cancel(self):
        pos={"strategy":live.STRATEGY,"inst":"ABC-USDT","side":"LONG",
             "reference_entry":"100","tp":"100.55","sl":"99.50",
             "tp_policy":"TP055","sl_policy":"SL05","tpsl_id":"legacy"}
        self.bot.exchange_tpsl["ABC-USDT"]={
             "state":"live","tpslId":"legacy","tpTriggerPrice":"100.55",
             "slTriggerPrice":"99.50","tpTriggerPriceType":"last",
             "slTriggerPriceType":"last"}
        self.assertTrue(live._ensure_protection(self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertTrue(live._ensure_protection(self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertEqual(self.bot.amend_calls,1)
        self.assertEqual([x[1] for x in self.bot.posted],["/api/v1/trade/amend-tpsl"])
        self.assertEqual(pos["protection_status"],"TP055_SL05_MARK_ACTIVE")
        self.assertEqual(pos["tpsl_id"],"legacy")

    def test_uncertain_amend_not_sent_twice(self):
        pos={"strategy":live.STRATEGY,"inst":"ABC-USDT","side":"LONG",
             "reference_entry":"100","tpsl_id":"legacy"}
        self.bot.exchange_tpsl["ABC-USDT"]={
             "state":"live","tpslId":"legacy","tpTriggerPrice":"100.55",
             "slTriggerPrice":"99.50","tpTriggerPriceType":"last",
             "slTriggerPriceType":"last"}
        self.bot.amend_uncertain=True
        self.bot.defer_amend=True
        self.assertFalse(live._ensure_protection(self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertFalse(live._ensure_protection(self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertEqual(self.bot.amend_calls,1)
        self.bot.exchange_tpsl["ABC-USDT"].update(tpTriggerPriceType="mark",slTriggerPriceType="mark")
        self.assertTrue(live._ensure_protection(self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertEqual(self.bot.amend_calls,1)

    def test_missing_broker_order_blocks_not_replaces(self):
        pos={"strategy":live.STRATEGY,"inst":"ABC-USDT","side":"LONG",
             "reference_entry":"100","tpsl_id":"missing"}
        self.assertFalse(live._ensure_protection(self.bot,self.state,pos,self.instruments["ABC-USDT"]))
        self.assertEqual(self.bot.posted,[])

    def test_tick_rounding_never_exceeds_stop(self):
        x=live._protection_prices(self.bot,{"side":"LONG",
          "reference_entry":"0.07003"},{"tickSize":"0.0001"})
        y=live._protection_prices(self.bot,{"side":"SHORT",
          "reference_entry":"0.07003"},{"tickSize":"0.0001"})
        self.assertEqual(tuple(map(str,x)),("0.0705","0.0697"))
        self.assertEqual(tuple(map(str,y)),("0.0696","0.0703"))

    @patch("blofin_trade_charts.capture_chart",side_effect=RuntimeError("no rendering in offline tests"))
    def test_paused_does_not_open_new_but_closes_old_short(self,chart):
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("XYZ-USDT","SHORT"),self.instruments))
        self.state["positions"]["XYZ-USDT"]["last_same_volume"]=60
        self.state["positions"]["XYZ-USDT"]["last_checked_signal_close_ms"]=T
        b=bars("LONG")
        b.append(candle(T,o=100,c=100.1,volume=80))
        # A qualified green at next completed boundary; state exit must happen
        def confirm(insts,close):
            return {"XYZ-USDT":b,"ABC-USDT":bars("SHORT")+[candle(T)],
                    "BTC-USDT":bars("LONG")+[candle(T)]}
        self.bot.now=T+F+1000
        rank=["ABC-USDT"]+[f"COIN{i}-USDT" for i in range(199)]
        with patch.dict("os.environ",{"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES":"true"}):
            live.run(self.bot,self.state,rank,{},self.instruments,T+F,confirm)
        # fake broker support will mark the close; no additional orders
        self.assertFalse(any(a[2].get("instId")=="ABC-USDT" for a in self.bot.posted))
    def test_old_strategy_position_unmodified_during_paused_scan(self):
        self.state["positions"]["LEGACY-USDT"]={"inst":"LEGACY-USDT","side":"LONG",
            "strategy":"RED_SMA_REBOUND","hold_policy":"HOLD_60M",
            "tp":"105","sl":"99","notional_usdt":"5"}
        self.bot.account["LEGACY-USDT"]=True
        rank=["ABC-USDT"]+[f"COIN{i}-USDT" for i in range(199)]
        def confirm(ids,ms):return {"LEGACY-USDT":bars("LONG")}
        with patch.dict("os.environ",{"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES":"true"}):
            live.run(self.bot,self.state,rank,{},self.instruments,T,confirm)
        self.assertEqual(self.state["positions"]["LEGACY-USDT"]["tp"],"105")
        self.assertEqual(self.state["positions"]["LEGACY-USDT"]["sl"],"99")

class TunnelBrokerProtectionRegression(unittest.TestCase):
    """LIVE state sync must never cancel a tunnel-protected broker OCO."""

    def test_cancel_tpsl_keeps_both_tunnel_sides(self):
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                pos = {
                    "inst": "XTZ-USDT", "side": side,
                    "strategy": live.STRATEGY,
                    "hold_policy": live.HOLD_POLICY,
                    "tp_policy": "TUNNEL_LINE", "sl_policy": "TUNNEL_LINE",
                    "tp_sl_mode": live.TUNNEL_TPSL_MODE,
                    "tpsl_id": "live-oco-id",
                    "tpsl_client_order_id": "live-client-id",
                    "tp": "101", "sl": "99",
                    "protection_status": "TUNNEL_LINES_MARK_ACTIVE",
                }
                state = {"position": pos}
                with patch.object(hourly, "private_request") as broker:
                    self.assertFalse(hourly.cancel_tracked_tpsl(state))
                    broker.assert_not_called()
                self.assertEqual(pos["tpsl_id"], "live-oco-id")
                self.assertEqual(pos["tp"], "101")
                self.assertEqual(pos["sl"], "99")
                self.assertEqual(pos["protection_status"],
                                 "TUNNEL_LINES_MARK_ACTIVE")

    def test_sync_all_positions_does_not_cancel_tunnel_tpsl(self):
        pos = {
            "inst": "XTZ-USDT", "side": "LONG",
            "strategy": live.STRATEGY,
            "hold_policy": live.HOLD_POLICY,
            "tp_policy": "TUNNEL_LINE", "sl_policy": "TUNNEL_LINE",
            "tp_sl_mode": live.TUNNEL_TPSL_MODE,
            "tpsl_id": "live-oco-id", "tpsl_client_order_id": "client",
            "tp": "101", "sl": "99",
            "protection_status": "TUNNEL_LINES_MARK_ACTIVE",
        }
        state = {"positions": {"XTZ-USDT": pos}}
        with patch.object(hourly, "get_open_positions",
                          return_value=[{"instId": "XTZ-USDT"}]):
            with patch.object(hourly, "private_request") as broker:
                hourly.sync_all_tracked_positions(state)
                broker.assert_not_called()
        self.assertEqual(state["positions"]["XTZ-USDT"]["tpsl_id"],
                         "live-oco-id")
        self.assertEqual(state["positions"]["XTZ-USDT"]["tp_policy"],
                         "TUNNEL_LINE")



class EmergencyProtectionRegression(unittest.TestCase):
    """Broker-verified emergency close prevents unchecked unprotected positions."""

    def test_verified_emergency_close_starts_once(self):
        from unittest.mock import Mock
        state={"positions":{"ARKM-USDT":{"inst":"ARKM-USDT","side":"LONG",
            "opened_ms":1000,"filled_size":"42"}}}
        bot=Mock()
        bot.get_tracked_positions.side_effect=lambda st: st["positions"]
        bot.d=lambda value: Decimal(str(value))
        bot.now_ms.return_value=123456
        bot.get_open_positions.return_value=[{"instId":"ARKM-USDT",
            "positions":"42","marginMode":"isolated","positionSide":"net"}]
        self.assertTrue(live._emergency_close_unprotected(bot,state,"ARKM-USDT"))
        self.assertEqual(bot._run_for_tracked_position.call_count,1)
        self.assertEqual(state["positions"]["ARKM-USDT"]["exit_pending"],
                         "AWAITING_BROKER_FLAT")
        self.assertFalse(live._emergency_close_unprotected(bot,state,"ARKM-USDT"))
        self.assertEqual(bot._run_for_tracked_position.call_count,1)

    def test_emergency_close_never_touches_unverified_broker_position(self):
        from unittest.mock import Mock
        state={"positions":{"ARKM-USDT":{"inst":"ARKM-USDT","side":"LONG",
            "opened_ms":1000,"filled_size":"42"}}}
        bot=Mock()
        bot.get_tracked_positions.side_effect=lambda st: st["positions"]
        bot.d=lambda value: Decimal(str(value))
        bot.get_open_positions.return_value=[{"instId":"ARKM-USDT",
            "positions":"-42","marginMode":"isolated","positionSide":"net"}]
        self.assertFalse(live._emergency_close_unprotected(bot,state,"ARKM-USDT"))
        bot._run_for_tracked_position.assert_not_called()
        self.assertNotIn("exit_pending",state["positions"]["ARKM-USDT"])

class PrivateGet429Recovery(unittest.TestCase):
    """No LIVE orders: authenticated GET backoff must never duplicate a trade."""

    def test_private_get_429_recovers_with_new_signature(self):
        from unittest.mock import Mock
        blocked = Mock(status_code=429, headers={"Retry-After": "0"})
        ok = Mock(status_code=200, headers={})
        ok.json.return_value = {"code": "0", "data": [{"instId": "MOCK-USDT"}]}
        with patch.object(hourly.requests, "get", side_effect=[blocked, ok]) as req:
            with patch.object(hourly, "sign", side_effect=[
                    {"ACCESS-NONCE": "first"}, {"ACCESS-NONCE": "second"}]) as sign:
                with patch.object(hourly.time, "sleep") as wait:
                    hourly._private_get_last_at = 0.0
                    result = hourly.private_request(
                        "GET", "/api/v1/account/positions")
        self.assertEqual(result, [{"instId": "MOCK-USDT"}])
        self.assertEqual(req.call_count, 2)
        self.assertEqual(sign.call_count, 2)
        self.assertTrue(wait.called)

    def test_private_get_429_exhausted_raises(self):
        from unittest.mock import Mock
        import requests
        blocked = Mock(status_code=429, headers={})
        blocked.raise_for_status.side_effect = requests.HTTPError("429 Too Many Requests")
        with patch.object(hourly.requests, "get", return_value=blocked) as req:
            with patch.object(hourly.time, "sleep"):
                hourly._private_get_last_at = 0.0
                with self.assertRaises(requests.HTTPError):
                    hourly.private_request("GET", "/api/v1/account/positions")
        self.assertEqual(req.call_count, hourly.PRIVATE_GET_429_ATTEMPTS)

    def test_private_post_429_never_retried(self):
        from unittest.mock import Mock
        import requests
        blocked = Mock(status_code=429)
        blocked.raise_for_status.side_effect = requests.HTTPError("429 Too Many Requests")
        with patch.object(hourly.requests, "request", return_value=blocked) as req:
            with patch.object(hourly.time, "sleep") as wait:
                with self.assertRaises(requests.HTTPError):
                    hourly.private_request("POST", "/api/v1/trade/order",
                                           body={"instId": "MOCK-USDT"})
        req.assert_called_once()
        wait.assert_not_called()


if __name__=="__main__":
    unittest.main()
