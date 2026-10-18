"""Offline unit tests: no BloFin credentials, no network and no orders."""
import unittest
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import blofin_red_sma_rebound_live as target


class FakeBot:
    SIGNAL_MAX_AGE_MS=180000
    SIGNAL_MINUTES=5
    SIGNAL_LABEL="5m"
    TOP_N=200
    MAX_NOTIONAL_USDT=Decimal("25")
    MARGIN_MODE="isolated"
    CODE_COMMIT="unit-test"
    def __init__(self):
        self.now=1800000000000
        self.posts=[]
        self.notifications=[]
        self.state_written=0
        self.account_open=[]
        self.positions={}
        self.free=Decimal("25")
        self.bankroll=Decimal("25")
        self.risk=False
    def now_ms(self):return self.now
    def bar_close_ms(self,b):return int(b["ts"])+target.CANDLE_MS
    def risk_stop_active(self,state):return self.risk
    def get_tracked_positions(self,state):return state.setdefault("positions",{})
    def get_open_positions(self):return self.account_open
    def get_available_usdt(self):return self.free
    def current_live_bankroll(self,state):return self.bankroll
    def d(self,x):return Decimal(str(x))
    def clean_decimal(self,x):return format(x,"f") if isinstance(x,Decimal) else str(x)
    def price_step(self,price,tick,mode):
        price=self.d(price);tick=self.d(tick)
        return (price/tick).to_integral_value(rounding=mode)*tick
    def checked_preorder_quote(self,inst,side,ref,enforce_adverse_gap=True):
        return {"expected_fill":"97.40","bid":"97.39","ask":"97.40"}
    def size_for_notional(self,price,meta,cap):
        if cap<20:return None
        return Decimal("0.2"),Decimal("19.48")
    def set_one_x(self,inst):pass
    def save_state(self,state):self.state_written+=1
    def private_request(self,method,path,body):
        self.posts.append((method,path,body))
        return [{"code":"0","orderId":"1234"}]
    def wait_for_order_fill(self,inst,order_id,client_id):
        return {"averagePrice":"97.40","filledSize":"0.2","state":"filled","fee":"-0.01"}
    def place_tpsl_for_position(self,inst,side,tp,sl):
        self.posts.append(("POST","/api/v1/trade/order-tpsl",{"tp":tp,"sl":sl}))
        return "tpsl-123","client-123"
    def notify(self,message,title,attachment_path=None):
        self.notifications.append((title,message))
    def _run_for_tracked_position(self,state,inst,func,*args):return None
    def sync_all_tracked_positions(self,state):pass
    def evaluate_all_tracked_exit_signals(self,state,expected_close_ms):pass


def good_candles():
    now=1800000000000
    starts=[now-11*target.CANDLE_MS+i*target.CANDLE_MS for i in range(11)]
    bars=[{"ts":ts,"o":100.0,"h":100.4,"l":99.5,"c":100.0,"v":500.0}
          for ts in starts]
    bars[-2].update({"o":98.8,"h":99.0,"l":98.2,"c":98.4})
    bars[-1].update({"o":98.0,"h":98.1,"l":97.1,"c":97.4})
    return bars


class Signals(unittest.TestCase):
    def setUp(self):
        self.bot=FakeBot()
        self.bars=good_candles()
        self.at=self.bars[-1]["ts"]+target.CANDLE_MS
    def test_match_exact_two_red_and_distance(self):
        s=target.closed_candle_signal(self.bot,self.bars,self.at)
        self.assertIsNotNone(s)
        self.assertEqual(s["side"],"LONG")
        self.assertGreaterEqual(s["signal_red_body_pct"],.4)
        self.assertLess(s["signal_red_body_pct"],.8)
        self.assertGreaterEqual(s["signal_close_below_sma10_pct"],2)
    def test_no_future_or_unconfirmed_candle(self):
        self.assertIsNone(target.closed_candle_signal(self.bot,self.bars,self.at+300000))
        self.assertIsNone(target.closed_candle_signal(self.bot,self.bars[:-1],self.at))
        missing=[dict(b) for b in self.bars]
        missing[-2]["ts"]-=300000
        self.assertIsNone(target.closed_candle_signal(self.bot,missing,self.at))
    def test_wrong_colour_or_body(self):
        invalid=[dict(b) for b in self.bars]
        invalid[-2]["c"]=99.0
        self.assertIsNone(target.closed_candle_signal(self.bot,invalid,self.at))
        invalid=[dict(b) for b in self.bars]
        invalid[-1]["c"]=97.0  # body >=0.8%
        self.assertIsNone(target.closed_candle_signal(self.bot,invalid,self.at))
        invalid=[dict(b) for b in self.bars]
        invalid[-1]["c"]=97.9
        self.assertIsNone(target.closed_candle_signal(self.bot,invalid,self.at))
    def test_reject_not_below_sma_2pct(self):
        bars=[dict(b) for b in self.bars]
        for bar in bars[:-2]:
            bar["c"]=98.0
        self.assertIsNone(target.closed_candle_signal(self.bot,bars,self.at))
    def test_invalid_ohlc_fails_closed(self):
        bars=[dict(b) for b in self.bars]
        bars[-1]["c"]=float("nan")
        self.assertIsNone(target.closed_candle_signal(self.bot,bars,self.at))
    def test_targets_and_bad_tick(self):
        tp,sl=target.broker_targets(self.bot,"100",".01")
        self.assertEqual(tp,Decimal("101.50"))
        self.assertEqual(sl,Decimal("99.00"))
        with self.assertRaises(ValueError):
            target.broker_targets(self.bot,".2",".1")


class Entry(unittest.TestCase):
    def setUp(self):
        self.bot=FakeBot()
        self.bot.now=1800000000000+target.CANDLE_MS
        self.meta={"EPIC-USDT":{"tickSize":".01","contractValue":"1"}}
        self.sig={"inst":"EPIC-USDT","side":"LONG","close":97.4,
                  "signal_close_ms":self.bot.now-1000}
        self.state={"positions":{}}
    def test_minimum_free_equity_20p50_buffer(self):
        self.bot.free=Decimal("20.49")
        self.assertFalse(target.open_long(self.bot,self.state,self.sig,1,self.meta))
        self.assertFalse(self.bot.posts)
    def test_25_usdt_target_not_permanent_free_equity_floor(self):
        self.bot.free=Decimal("24.99")
        self.assertTrue(target.open_long(self.bot,self.state,self.sig,1,self.meta))
        self.assertEqual(len(self.bot.posts),2)
    def test_hard_drawdown_stop_blocks(self):
        self.bot.risk=True
        self.assertFalse(target.open_long(self.bot,self.state,self.sig,1,self.meta))
        self.assertFalse(self.bot.posts)
    def test_any_other_position_blocks(self):
        self.bot.account_open=[{"instId":"BTC-USDT"}]
        self.assertFalse(target.open_long(self.bot,self.state,self.sig,1,self.meta))
        self.assertFalse(self.bot.posts)
    def test_no_old_signal_or_oversized_budget(self):
        self.sig["signal_close_ms"]=self.bot.now-130001
        self.assertFalse(target.open_long(self.bot,self.state,self.sig,1,self.meta))
        self.assertFalse(self.bot.posts)
    def test_protected_market_order_tp15_sl1_hold60(self):
        self.assertTrue(target.open_long(self.bot,self.state,self.sig,1,self.meta))
        self.assertEqual(len(self.bot.posts),2)
        self.assertEqual(self.bot.posts[0][1],"/api/v1/trade/order")
        self.assertEqual(self.bot.posts[0][2]["side"],"buy")
        self.assertEqual(self.bot.posts[0][2]["size"],"0.2")
        self.assertEqual(self.bot.posts[1][1],"/api/v1/trade/order-tpsl")
        pos=self.state["positions"]["EPIC-USDT"]
        self.assertEqual(pos["hold_policy"],"HOLD_60M")
        self.assertEqual(pos["strategy"],target.STRATEGY)
        self.assertEqual(pos["tp"],"98.87")
        self.assertEqual(pos["sl"],"96.43")
        self.assertEqual(pos["protection_status"],"BROKER_TPSL_ACTIVE")
        self.assertEqual(self.state["red_sma_order_attempt"]["status"],"PROTECTED")
    def test_rejected_tp_price_triggers_safety(self):
        # Coarse price tick prevents a valid stop and cannot open a position.
        self.meta["EPIC-USDT"]["tickSize"]="100"
        self.assertFalse(target.open_long(self.bot,self.state,self.sig,1,self.meta))
        self.assertFalse(self.bot.posts)


if __name__=="__main__":unittest.main()
