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

class FakeBot(prior.FakeBot):
    def __init__(self):
        super().__init__()
        self.now=T+1000
        self.exchange_tpsl={}
        self.amend_calls=0
        self.amend_uncertain=False
        self.defer_amend=False
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

class Broker(unittest.TestCase):
    def setUp(self):
        self.bot=FakeBot()
        self.state={"positions":{}}
        self.instruments={"ABC-USDT":{"contractValue":"1","tickSize":"0.01"},
                          "XYZ-USDT":{"contractValue":"1","tickSize":"0.01"}}
    def signal(self,coin,side):
        s=live.entry_signal(self.bot,bars(side),T)
        s.update(inst=coin,rank=1)
        return s
    @patch("blofin_trade_charts.capture_chart",side_effect=RuntimeError("no rendering in offline tests"))
    def test_buy_long_sell_short_each_one_per_coin_tpsl(self,chart):
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("ABC-USDT","LONG"),self.instruments))
        self.assertTrue(live._open_one(self.bot,self.state,self.signal("XYZ-USDT","SHORT"),self.instruments))
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
        self.assertEqual(pos["protection_status"],"TP055_SL05_ACTIVE")


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
