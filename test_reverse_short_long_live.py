"""Offline tests for reverse-SHORT to LONG live module: never send real orders."""
import unittest
from decimal import Decimal
from unittest.mock import patch

import blofin_reverse_short_long_live as v

F=v.FIVE
T=(1800000000000//(F*3))*(F*3)
TS=T-F

def row(ts, o=100.0, h=100.2, l=99.8, c=100.0, volume=100.0):
    return {"ts":ts,"o":o,"h":h,"l":l,"c":c,"v":volume}

def good_bars():
    bars=[row(T-(200-i)*F) for i in range(200)]
    # last candle close=T: RED after immediate GREEN
    bars[-31]["c"]=99.8  # 150m earlier: the signal close must be >= this\n    bars[-2]=row(T-2*F,100.0,100.15,99.9,100.10,20.0)
    bars[-1]=row(T-F,100.1,100.2,99.8,99.99,70.0)
    return bars

def good_btc():
    return [row(T-(200-i)*F) for i in range(200)]

class FakeBot:
    SIGNAL_MINUTES=5
    TOP_N=200
    SIGNAL_MAX_AGE_MS=180000
    MAX_NOTIONAL_USDT=Decimal("25")
    MAX_OPEN_POSITIONS=4
    MARGIN_MODE="isolated"
    CODE_COMMIT="offline-test"
    def __init__(self):
        self.now=T+1000
        self.saved=0
        self.posted=[]
        self.positions={}
        self.account={}
        self.size_by_coin={}
        self.closed=[]
        self.notify_messages=[]
    def now_ms(self):return self.now
    def bar_close_ms(self,bar):return int(bar["ts"])+F
    def risk_stop_active(self,state):return False
    def get_tracked_positions(self,state):return state.setdefault("positions",{})
    def get_open_positions(self):
        return [{"instId":coin,"positions":"1"} for coin in self.account]
    def current_live_bankroll(self,state):return Decimal("25")
    def get_available_usdt(self):return Decimal("25")
    def checked_preorder_quote(self,inst,side,close,enforce_adverse_gap=True):
        return {"expected_fill":"100","ask":"100","bid":"99.99","quote_age_ms":100}
    def d(self,n):return Decimal(str(n))
    def clean_decimal(self,x):return str(x)
    def size_for_notional(self,price,meta,cap):
        size=(cap/Decimal("100")).quantize(Decimal("0.01"),rounding="ROUND_DOWN")
        return (size,size*Decimal("100")) if size>=Decimal("0.05") else None
    def set_one_x(self,inst):pass
    def save_state(self,state):self.saved+=1
    def private_request(self,method,path,body):
        self.posted.append((method,path,body))
        if path=="/api/v1/trade/order":
            self.size_by_coin[body["instId"]]=body["size"]
            self.account[body["instId"]]=True
        return [{"code":"0","orderId":"ok"+str(len(self.posted))}]
    def wait_for_order_fill(self,inst,order_id,client_id):
        return {"averagePrice":"100","filledSize":self.size_by_coin[inst],"fee":"-0.01"}
    def notify(self,*a,**kw):self.notify_messages.append(a)
    def sync_all_tracked_positions(self,state):pass
    def evaluate_all_tracked_exit_signals(self,state,expected_close_ms):pass
    def _run_for_tracked_position(self,state,inst,func,*args):
        pos=state["positions"][inst]
        self.closed.append(inst)
        del state["positions"][inst]
        self.account.pop(inst,None)


class EntryRules(unittest.TestCase):
    def setUp(self):
        self.bot=FakeBot()
        self.bars=good_bars()
        self.btc=good_btc()
    def signal(self,bars=None,btc=None):
        return v.entry_signal(self.bot,self.bars if bars is None else bars,
                              self.btc if btc is None else btc,T)
    def test_exact_long_setup(self):
        s=self.signal()
        if s is None:
            a=v._by_ts(self.bars,T)
            bb=v._by_ts(self.btc,T)
            cur=a.get(T-F);prev=a.get(T-2*F);old=a.get(T-31*F)
            print("OFFLINE_ENTRY_DEBUG",{"T":T,"cur":cur,"previous":prev,
                "past150":old,"mean":v._previous10_mean(a,T-F),
                "last15":v._completed_coin15_pct(a,T-F),
                "btc":bb.get(T-F),"btc_old":bb.get(T-4*F)},flush=True)
        self.assertEqual(s["side"],"LONG")
        self.assertEqual(s["signal_red_volume"],70.)
        self.assertEqual(s["volume_ratio"],3.5)
        self.assertLessEqual(s["btc_prior15_pct"],.062055813)
        self.assertGreaterEqual(s["coin_completed15_pct"],-.10322961)
    def test_4x_exact_rejected(self):
        a=good_bars();a[-1]["v"]=80.;self.assertIsNone(self.signal(a))
    def test_below_3x_rejected(self):
        a=good_bars();a[-1]["v"]=59.;self.assertIsNone(self.signal(a))
    def test_above_mean10_rejected(self):
        a=good_bars()
        for bar in a[-11:-2]:bar["v"]=20.
        a[-1]["v"]=70.
        self.assertIsNone(self.signal(a))
    def test_insidered_rejected(self):
        a=good_bars();a[-1].update({"h":100.1,"l":99.91});self.assertIsNone(self.signal(a))
    def test_btc_plus_062_blocked(self):
        btc=good_btc();btc[-1]["c"]=100.07
        self.assertIsNone(self.signal(btc=btc))
    def test_negative_btc_allowed(self):
        btc=good_btc();btc[-1]["c"]=99.9
        self.assertIsNotNone(self.signal(btc=btc))
    def test_last_completed_15m_below_threshold_blocked(self):
        a=good_bars()
        # For last 5m of 15m block, completed15 includes signal.
        a[-1]["c"]=99.8
        self.assertIsNone(self.signal(a))
    def test_coin_150m_negative_rejected(self):
        a=good_bars();a[-31]["c"]=102
        self.assertIsNone(self.signal(a))
    def test_missing_previous_or_stale(self):
        self.assertIsNone(self.signal(bars=self.bars[-30:]))
        self.assertIsNone(v.entry_signal(self.bot,self.bars,self.btc,T+F))
        a=good_bars();a.pop(-30)
        self.assertIsNone(self.signal(a))
    def test_exit_green_gt_last_qualifying_red(self):
        pos={"last_red_volume":70}
        a=good_bars()
        a[-1]=row(T-F,100,100.25,99.9,100.1,80)
        close,new=v.exit_transition(pos,a,T)
        self.assertTrue(close);self.assertEqual(new,70)
    def test_exit_green_equal_red_no_exit(self):
        pos={"last_red_volume":70}
        a=good_bars()
        a[-1]=row(T-F,100,100.25,99.9,100.1,70)
        self.assertEqual(v.exit_transition(pos,a,T),(False,70))
    def test_exit_ignore_volume_above_mean10(self):
        pos={"last_red_volume":70}
        a=good_bars()
        a[-1]=row(T-F,100,100.25,99.9,100.1,150)
        self.assertEqual(v.exit_transition(pos,a,T),(False,70))
    def test_exit_qualifying_red_updates_marker(self):
        pos={"last_red_volume":70}
        a=good_bars()
        a[-1]=row(T-F,100.1,100.2,99.8,99.95,50)
        self.assertEqual(v.exit_transition(pos,a,T),(False,50))
    def test_no_future_for_entry(self):
        a=good_bars()
        a.append(row(T+F,o=5,h=6,l=4,c=5,volume=1))
        self.assertEqual(self.signal(a),self.signal())


class BrokerOffline(unittest.TestCase):
    def setUp(self):
        self.bot=FakeBot()
        self.instruments={"XMR-USDT":{"contractValue":"1"},
                          "ARC-USDT":{"contractValue":"1"}}
        self.state={"positions":{}}
    def sig(self,coin):
        s=v.entry_signal(self.bot,good_bars(),good_btc(),T)
        s["inst"]=coin;s["rank"]=1
        return s
    @patch("blofin_trade_charts.capture_chart",side_effect=RuntimeError("no image in unit"))
    def test_two_different_coins_no_tp_sl(self,chart):
        self.assertTrue(v._open_one(self.bot,self.state,self.sig("XMR-USDT"),self.instruments))
        self.assertTrue(v._open_one(self.bot,self.state,self.sig("ARC-USDT"),self.instruments))
        self.assertEqual(len(self.state["positions"]),2)
        self.assertFalse(v._open_one(self.bot,self.state,self.sig("XMR-USDT"),self.instruments))
        self.assertEqual(len(self.bot.posted),2)
        self.assertTrue(all(path=="/api/v1/trade/order" for _,path,_ in self.bot.posted))
        self.assertTrue(all(p["tp"]=="" and p["sl"]=="" for p in self.state["positions"].values()))
        self.assertTrue(all(p["hold_policy"]==v.HOLD_POLICY for p in self.state["positions"].values()))
        notionals=sum(Decimal(x["notional_usdt"]) for x in self.state["positions"].values())
        self.assertLessEqual(notionals,Decimal("25"))
    @patch("blofin_trade_charts.capture_chart",side_effect=RuntimeError("no image in unit"))
    def test_run_detects_exit_even_outside_top200(self,chart):
        self.assertTrue(v._open_one(self.bot,self.state,self.sig("XMR-USDT"),self.instruments))
        old=self.state["positions"]["XMR-USDT"]
        old["last_checked_signal_close_ms"]=T
        # Next 5m candle: green volume larger than last_red & <= historical mean.
        b=good_bars()
        new=row(T,100,100.2,99.8,100.1,80)
        b.append(new)
        nxt=T+F
        ranks=["ARC-USDT"]+[f"OTHER{i}-USDT" for i in range(199)]
        def confirm(ids,ts):
            self.assertIn("XMR-USDT",ids)
            return {"XMR-USDT":b,"BTC-USDT":good_btc()+[row(T)],
                    "ARC-USDT":good_bars()+[row(T)]}
        self.bot.now=nxt+1000
        v.run(self.bot,self.state,ranks,{},self.instruments,nxt,confirm)
        self.assertEqual(self.bot.closed,["XMR-USDT"])
    @patch("blofin_trade_charts.capture_chart",side_effect=RuntimeError("no image in unit"))
    def test_no_tp_sl_old_position_unchanged(self,chart):
        self.state["positions"]["OTHER-USDT"]={
            "inst":"OTHER-USDT","side":"LONG","hold_policy":"HOLD_60M",
            "tp":"101","sl":"99","strategy":"LEGACY","notional_usdt":"5"}
        self.bot.account["OTHER-USDT"]=True
        def confirm(ids,ts):
            return {"BTC-USDT":good_btc()}
        ranks=["ARC-USDT"]+[f"OTHER{i}-USDT" for i in range(199)]
        v.run(self.bot,self.state,ranks,{},self.instruments,T,confirm)
        p=self.state["positions"]["OTHER-USDT"]
        self.assertEqual((p["tp"],p["sl"]),("101","99"))
        self.assertEqual(p["strategy"],"LEGACY")

if __name__=="__main__":unittest.main()
