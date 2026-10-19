"""Offline tests: JSON volume candle audit, exact trigger and closed history.

No LIVE credentials, exchange requests or orders.
"""
import copy
import json
import unittest
from unittest.mock import patch

import blofin_volume_flip_both_live as live
from test_volume_flip_both_live import FakeBot, F, T, bars, candle


def pos(side="LONG",volume=80):
    return {
        "inst":"ABC-USDT", "side":side,
        "strategy":live.STRATEGY, "hold_policy":live.HOLD_POLICY,
        "signal_close_ms":T,"opened_ms":T+1000,
        "signal_snapshot":{"signal_volume":volume},
        "last_checked_signal_close_ms":T,
    }


class VolumeJournal(unittest.TestCase):
    def test_long_consecutive_and_opposite_candle_counts(self):
        p=pos("LONG")
        candles=[candle(T,o=100,c=100.2,volume=50),
                 candle(T+F,o=100,c=100.3,volume=65),
                 candle(T+2*F,o=100.4,c=100.0,volume=70),
                 candle(T+3*F,o=100,c=100.2,volume=90)]
        a=live._audit_confirmed_volume_candles(p,candles,T+4*F)
        self.assertEqual(a["entry_signal_candle"],{"close_ms":T,"color":"GREEN","volume":80})
        self.assertEqual([x["volume"] for x in a["candles"]],[50.,65.,70.,90.])
        self.assertEqual([x["color"] for x in a["candles"]],["GREEN","GREEN","RED","GREEN"])
        self.assertEqual(a["total_green_bars"],3)
        self.assertEqual(a["total_red_bars"],1)
        self.assertEqual(a["total_opposite_bars"],1)
        self.assertEqual(a["max_green_streak"],2)
        self.assertEqual(a["max_red_streak"],1)
        self.assertEqual(a["current_green_streak"],1)
        self.assertEqual(a["missing_close_ms"],[])
        self.assertEqual(a["audited_through_close_ms"],T+4*F)
        json.dumps(p)
        # No double counting on a repeated GitHub Actions scan.
        again=live._audit_confirmed_volume_candles(p,candles,T+4*F)
        self.assertEqual(len(again["candles"]),4)
        self.assertEqual(again["total_green_bars"],3)

    def test_short_all_opposing_green_bar_total_and_max(self):
        p=pos("SHORT")
        cc=[candle(T,o=100,c=99,volume=51),
            candle(T+F,o=99,c=100,volume=66),
            candle(T+2*F,o=100,c=101,volume=77),
            candle(T+3*F,o=101,c=102,volume=88),
            candle(T+4*F,o=102,c=100,volume=99)]
        a=live._audit_confirmed_volume_candles(p,cc,T+5*F)
        self.assertEqual(a["total_opposite_bars"],3)
        self.assertEqual(a["max_opposite_streak"],3)
        self.assertEqual(a["max_green_streak"],3)
        self.assertEqual(a["total_trade_direction_bars"],2)
        self.assertEqual(a["current_red_streak"],1)

    def test_missing_candle_breaks_streak_and_backfills_later(self):
        p=pos("SHORT")
        later=[candle(T+F,o=100,c=101,volume=50),
               candle(T+2*F,o=101,c=102,volume=40)]
        a=live._audit_confirmed_volume_candles(p,later,T+3*F)
        self.assertEqual(a["missing_close_ms"],[T+F])
        self.assertEqual(a["max_green_streak"],2)
        a=live._audit_confirmed_volume_candles(
            p,[candle(T,o=100,c=101,volume=70)]+later,T+3*F)
        self.assertEqual(a["missing_close_ms"],[])
        self.assertEqual(a["max_green_streak"],3)
        self.assertEqual(a["total_opposite_bars"],3)
        self.assertEqual(len(a["candles"]),3)

    def test_future_unclosed_candle_and_doji_are_not_counted_as_direction(self):
        p=pos("LONG")
        a=live._audit_confirmed_volume_candles(
            p,[candle(T,o=100,c=100,volume=10),
               candle(T+F,o=100,c=101,volume=20),
               candle(T+2*F,o=100,c=101,volume=30)],T+2*F)
        self.assertEqual([x["color"] for x in a["candles"]],["DOJI","GREEN"])
        self.assertEqual(len(a["candles"]),2)
        self.assertEqual(a["total_doji_bars"],1)
        self.assertEqual(a["max_green_streak"],1)

    def test_long_and_short_broker_close_snapshot_contains_trigger_volume(self):
        for side in ("LONG","SHORT"):
            with self.subTest(side=side):
                bot=FakeBot()
                bot.now=T+F+1000
                state={"positions":{"ABC-USDT":pos(side)}}
                bot.account["ABC-USDT"]=True
                captured=[]
                original=bot._run_for_tracked_position
                def wrapper(st,inst,fn,*args):
                    captured.append(copy.deepcopy(st["positions"][inst]["volume_candle_audit"]))
                    return original(st,inst,fn,*args)
                bot._run_for_tracked_position=wrapper
                current=(candle(T,o=101,c=100,volume=123) if side=="LONG"
                         else candle(T,o=100,c=101,volume=123))
                confirmed={"ABC-USDT":(bars(side)+[current])}
                def confirm(insts,expected):
                    self.assertIn("ABC-USDT",insts)
                    return confirmed
                ranked=["ZZZ-USDT"]+[f"M{i}-USDT" for i in range(199)]
                with patch.dict("os.environ",{"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES":"true"}):
                    live.run(bot,state,ranked,{}, {},T+F,confirm)
                self.assertNotIn("ABC-USDT",state["positions"])
                self.assertEqual(bot.closed,["ABC-USDT"])
                a=captured[0]
                self.assertEqual(a["exit_trigger_candle"]["volume"],123.)
                self.assertEqual(a["exit_trigger_candle"]["entry_signal_volume"],80)
                self.assertEqual(a["exit_trigger_candle"]["close_ms"],T+F)
                self.assertEqual(len(a["candles"]),1)
                self.assertEqual(a["total_opposite_bars"],1)

    def test_old_legacy_positions_unchanged_by_audit(self):
        p=pos("LONG")
        p["strategy"]=live.LEGACY_STRATEGY
        self.assertIsNone(live._audit_confirmed_volume_candles(
            p,[candle(T,o=100,c=101,volume=20)],T+F))
        self.assertNotIn("volume_candle_audit",p)

    def test_closed_trade_history_keeps_full_volume_audit(self):
        # This exercises the REAL history writer, with exchange history and
        # notification/chart services mocked. No orders or API calls.
        import blofin_live_hourly as bot
        p=pos("SHORT")
        p.update({"opened_ms":T+1000,"reference_entry":"100","notional_usdt":"5"})
        a=live._audit_confirmed_volume_candles(
            p,[candle(T,o=100,c=99,volume=10),
               candle(T+F,o=99,c=101,volume=40)],T+2*F)
        a["exit_trigger_candle"]={
            "close_ms":T+2*F,"color":"GREEN","volume":40,
            "entry_signal_volume":80}
        state={"position":p,"trade_history":[],"trades":{"total":0,"wins":0,"losses":0,"flat":0,"unverified":0}}
        h={"realizedPnl":"0.035","fee":"-0.006","openAveragePrice":"100",
           "closeAveragePrice":"99","updateTime":str(T+3*F),"historyId":"verified"}
        with patch.object(bot,"latest_position_history",return_value=h),\
             patch.object(bot,"append_pattern_audit"),\
             patch.object(bot,"notify"),\
             patch("blofin_trade_charts.capture_chart",side_effect=RuntimeError("offline no chart")):
            self.assertTrue(bot.record_closed(state,"volume test"))
        archive=state["trade_history"][-1]["volume_candle_audit"]
        self.assertEqual(archive["exit_trigger_candle"]["volume"],40)
        self.assertEqual(archive["total_opposite_bars"],1)
        self.assertEqual(archive["candles"][1]["volume"],40)
        self.assertIsNone(state["position"])
        self.assertEqual(state["trades"]["total"],1)
        json.dumps(state)


if __name__=="__main__":
    unittest.main()
