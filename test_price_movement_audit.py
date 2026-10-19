"""Offline-only tests for OHLC/price excursion journal; no real exchange calls.

The 5m high and low in the candle overlapping a LIVE fill can include prices
from BEFORE the fill, so results explicitly track both approximate and fully
post-fill candle-only extremes. Does not choose/modify TP or SL.
"""
import copy
import json
import unittest
from unittest.mock import patch

import blofin_volume_flip_both_live as live
from test_volume_candle_audit import pos
from test_volume_flip_both_live import F, T, candle


def c(ts,o,cl,h,l,vol):
    return {"ts":ts,"o":o,"c":cl,"h":h,"l":l,"v":vol}


class PriceMovement(unittest.TestCase):
    def test_long_actual_entry_favorable_and_adverse_extremes_and_partial_bar(self):
        p=pos("LONG")
        p["reference_entry"]="100.0"
        p["opened_ms"]=T+1000
        cc=[c(T,100,100.5,103,97,33),
            c(T+F,100.5,101.5,102,99,44)]
        audit=live._audit_confirmed_volume_candles(p,cc,T+2*F)
        path=audit["candles"]
        self.assertEqual([x["high"] for x in path],[103,102])
        self.assertEqual([x["low"] for x in path],[97,99])
        self.assertEqual([x["price_exposure"] for x in path],["ENTRY_PARTIAL","FULL"])
        self.assertEqual(path[0]["position_return_close_pct"],.5)
        self.assertEqual(path[1]["position_return_close_pct"],1.5)
        summary=p["price_movement"]
        self.assertEqual(summary["entry_price"],100)
        self.assertTrue(summary["contains_partial_entry_candle"])
        self.assertEqual(summary["max_favorable_move_pct_approx"],3.)
        self.assertEqual(summary["max_adverse_move_pct_approx"],3.)
        self.assertEqual(summary["max_favorable_move_pct_full_bars_only"],2.)
        self.assertEqual(summary["max_adverse_move_pct_full_bars_only"],1.)
        self.assertEqual(summary["best_price_5m_approx"],103)
        self.assertEqual(summary["worst_price_5m_approx"],97)
        self.assertEqual(summary["best_close_return_pct"],1.5)
        self.assertEqual(summary["fully_observed_candles"],1)
        json.dumps(p)

    def test_short_directional_price_moves_are_reversed(self):
        p=pos("SHORT")
        p["reference_entry"]="100.0"
        cc=[c(T,100,99,104,98,12),
            c(T+F,99,97,103,96,24)]
        audit=live._audit_confirmed_volume_candles(p,cc,T+2*F)
        self.assertEqual([r["position_return_close_pct"] for r in audit["candles"]],[1.,3.])
        summary=p["price_movement"]
        self.assertEqual(summary["max_favorable_move_pct_approx"],4.)
        self.assertEqual(summary["max_adverse_move_pct_approx"],4.)
        self.assertEqual(summary["max_favorable_move_pct_full_bars_only"],4.)
        self.assertEqual(summary["max_adverse_move_pct_full_bars_only"],3.)
        self.assertEqual(summary["best_price_5m_approx"],96)
        self.assertEqual(summary["worst_price_5m_approx"],104)
        self.assertEqual(summary["best_close_return_pct"],3.)

    def test_replay_and_missing_bars_never_invent_prices(self):
        p=pos("LONG")
        p["reference_entry"]="100"
        cc=[c(T+F,100,102,103,99,40)]
        a=live._audit_confirmed_volume_candles(p,cc,T+2*F)
        self.assertEqual(a["missing_close_ms"],[T+F])
        self.assertEqual(p["price_movement"]["fully_observed_candles"],1)
        self.assertEqual(p["price_movement"]["max_favorable_move_pct_full_bars_only"],3)
        self.assertEqual(len(a["candles"]),1)
        # A late/backfill candle arrives with the original open/close/volume;
        # earlier 5m bars archived before adding high/low fields are upgraded.
        a["candles"][0].pop("high")
        a["candles"][0].pop("low")
        a=live._audit_confirmed_volume_candles(
            p,[c(T,100,100.5,105,95,22)]+cc,T+2*F)
        self.assertEqual(len(a["candles"]),2)
        self.assertEqual(a["missing_close_ms"],[])
        self.assertEqual(a["candles"][1]["high"],103.)
        self.assertEqual(p["price_movement"]["max_favorable_move_pct_approx"],5)
        self.assertEqual(p["price_movement"]["max_adverse_move_pct_approx"],5)
        before=copy.deepcopy(a["candles"])
        live._audit_confirmed_volume_candles(p,[c(T,100,100.5,105,95,22)]+cc,T+2*F)
        self.assertEqual(p["volume_candle_audit"]["candles"],before)

    def test_no_valid_broker_entry_no_guessed_price_movement(self):
        p=pos("LONG")
        p.pop("reference_entry",None)
        a=live._audit_confirmed_volume_candles(
            p,[c(T,100,101,105,98,22)],T+F)
        self.assertIsNone(p.get("price_movement"))
        self.assertEqual(a["candles"][0]["volume"],22)

    def test_candle_ending_before_actual_fill_does_not_count(self):
        p=pos("LONG")
        p["reference_entry"]="100"
        p["opened_ms"]=T+F+1000
        a=live._audit_confirmed_volume_candles(
            p,[c(T,100,100,200,1,11),c(T+F,100,101,104,98,12)],T+2*F)
        self.assertEqual(a["candles"][0]["price_exposure"],"BEFORE_ENTRY")
        self.assertEqual(a["candles"][1]["price_exposure"],"ENTRY_PARTIAL")
        self.assertEqual(p["price_movement"]["max_favorable_move_pct_approx"],4)
        self.assertEqual(p["price_movement"]["max_adverse_move_pct_approx"],2)

    def test_trading_strategy_volume_exit_unchanged(self):
        p=pos("LONG",volume=80)
        p["reference_entry"]="100"
        b=[c(T,100,99,101,98,90)]
        live._audit_confirmed_volume_candles(p,b,T+F)
        self.assertEqual(live.exit_transition(p,b,T+F),(True,80))
        self.assertTrue(p["price_movement"]["max_adverse_move_pct_approx"]>=2)

    def test_broker_record_closed_archives_actual_exit_return_pct_and_5m_stats(self):
        import blofin_live_hourly as bot
        p=pos("SHORT")
        p.update({"reference_entry":"100", "notional_usdt":"5"})
        live._audit_confirmed_volume_candles(
            p,[c(T,100,99,101,96,12),c(T+F,99,98,100,94,18)],T+2*F)
        state={"position":p,"trade_history":[],
               "trades":{"total":0,"wins":0,"losses":0,"flat":0,"unverified":0}}
        hist={"realizedPnl":"0.035","fee":"-0.006",
              "openAveragePrice":"100","closeAveragePrice":"97",
              "updateTime":str(T+3*F),"historyId":"real"}
        with patch.object(bot,"latest_position_history",return_value=hist),\
             patch.object(bot,"append_pattern_audit"),\
             patch.object(bot,"notify"),\
             patch("blofin_trade_charts.capture_chart",side_effect=RuntimeError("no charts")):
            self.assertTrue(bot.record_closed(state,"exit"))
        saved=state["trade_history"][-1]
        movement=saved["price_movement"]
        self.assertEqual(movement["entry_price"],100)
        self.assertEqual(movement["actual_exit_price"],97)
        self.assertAlmostEqual(movement["actual_directional_return_pct"],3)
        self.assertEqual(movement["max_favorable_move_pct_approx"],6)
        self.assertEqual(movement["max_adverse_move_pct_approx"],1)
        self.assertEqual(movement["actual_gross_pnl_usdt"],.041)
        self.assertEqual(saved["volume_candle_audit"]["candles"][0]["low"],96)
        self.assertIsNone(state["position"])
        json.dumps(state)


if __name__=="__main__":
    unittest.main()
