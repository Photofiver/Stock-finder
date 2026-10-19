"""Offline no-credentials regression tests for live 5m volume-flip LONG AND SHORT."""
import unittest
from decimal import Decimal
from unittest.mock import patch
import blofin_volume_flip_both_live as live
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
        self.assertEqual(live.long_entry_extra_rejection(long_signal, 51),
                         "LONG_RANK_OUTSIDE_TOP50")
        self.assertEqual(live.long_entry_extra_rejection(long_signal, 0),
                         "LONG_RANK_OUTSIDE_TOP50")
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
        self.instruments={"ABC-USDT":{"contractValue":"1","tickSize":"0.01"},
                          "XYZ-USDT":{"contractValue":"1","tickSize":"0.01"}}
    def signal(self,coin,side):
        s=live.entry_signal(self.bot,bars(side),T)
        s.update(inst=coin,rank=1)
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

    def test_live_accepts_90_to_below_100_percent_of_prior_20_only(self):
        # Last signal candle's prior 20 bars average 98 (one red at vol60).
        # All setups pass the original red/green and increasing-volume rules.
        scenarios = (
            ("LOW-USDT", 88, "LONG"),       # 88/98 = 89.8%: reject
            ("MIN-USDT", 88.2, "SHORT"),     # 88.2/98 = 90%: accept
            ("MID-USDT", 94, "LONG"),       # 94/98 = 95.9%: accept
            ("UPPER-USDT", 98, "SHORT"),     # 98/98 = 100%: reject
            ("ABOVE-USDT", 101, "LONG"),    # 101/98 > 100%: reject
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
        self.assertEqual([x[0] for x in observed],["MID-USDT","MIN-USDT"])
        self.assertTrue(all(.9<=x[1]<1.0 for x in observed))
        scan=self.state["volume_long_last_scan"]
        self.assertEqual(scan["matched"],2)
        self.assertEqual(scan["volume_entry_strength_min_inclusive"],.90)
        self.assertEqual(scan["volume_entry_strength_max_exclusive"],1.0)
        self.assertEqual([x["inst"] for x in scan["candidates"]],
                         ["MID-USDT","MIN-USDT"])

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
            return {
                "GOOD-LONG": good,
                "RISING-LONG": rising,
                "GOOD-SHORT": bars("SHORT"),
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
            ("GOOD-LONG", "LONG", 1), ("GOOD-SHORT", "SHORT", 3)])
        scan = self.state["volume_long_last_scan"]
        self.assertEqual(scan["long_entry_rank_range_inclusive"], [1, 50])
        self.assertTrue(scan["long_requires_falling_prior_20bar_mean"])
        reasons = {x["inst"]: x["reason"] for x in scan["rejections"]}
        self.assertEqual(reasons["RISING-LONG"], "LONG_VOLUME_MA20_NOT_DECLINING")
        self.assertEqual(reasons["RANK51-LONG"], "LONG_RANK_OUTSIDE_TOP50")

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
        # First position targets 20 USDT, not half the bankroll.
        self.assertEqual(self.state["positions"]["ABC-USDT"]["notional_usdt"],"20.00")
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
        self.assertEqual((a["tp"],a["sl"]),("100.55","99.50"))
        self.assertEqual((b["tp"],b["sl"]),("99.45","100.50"))
        self.assertEqual((a["tp_policy"],b["sl_policy"]),("TP055","SL05"))
        self.assertEqual(self.bot.posted[1][2]["side"],"sell")
        self.assertEqual(self.bot.posted[3][2]["side"],"buy")
        self.assertFalse(live._open_one(self.bot,self.state,self.signal("ABC-USDT","LONG"),self.instruments))
        self.assertLessEqual(sum(Decimal(p["notional_usdt"]) for p in self.state["positions"].values()),Decimal("25"))
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


    def test_new_tpsl_uses_mark_trigger_both_directions(self):
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("ABC-USDT","LONG"),self.instruments))
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("XYZ-USDT","SHORT"),self.instruments))
        for coin in ("ABC-USDT","XYZ-USDT"):
            record=self.bot.exchange_tpsl[coin]
            self.assertEqual(record["tpTriggerPriceType"],"mark")
            self.assertEqual(record["slTriggerPriceType"],"mark")
            self.assertEqual(self.state["positions"][coin]["protection_status"],"TP055_SL05_MARK_ACTIVE")

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

if __name__=="__main__":
    unittest.main()
