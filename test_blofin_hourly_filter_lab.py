import json
import tempfile
import unittest
from pathlib import Path

from blofin_hourly_filter_lab import (
    flatten, entry_ok, load, freeze_bank, scan_bank, portfolio20,
)

class FilterLabTest(unittest.TestCase):
    def test_reject_future_data(self):
        s={"signal_close_ms":1000,"indicator_signal_close_ms":1000,"rsi14":51}
        self.assertTrue(entry_ok(s,1100))
        self.assertFalse(entry_ok(dict(s,indicator_signal_close_ms=1200),1100))
        self.assertFalse(entry_ok(dict(s,pivots=[{"confirmed_close_ms":1200}]),1100))

    def test_snapshot_has_no_secrets_or_exits(self):
        s={"rsi14":51,"macd_hist":2,"macd_hist_previous":1,
           "close":101,"sma10":100,"api_key":"DONT_COPY","exit_price":120}
        f=flatten(s)
        self.assertIn("derived.close_vs_sma10_pct",f)
        self.assertIn("derived.macd_change",f)
        self.assertNotIn("api_key",f)
        self.assertNotIn("exit_price",f)

    def test_history_validation_and_forward_frozen(self):
        source=[]
        for i in range(16):
            op=1000+i*300
            net=.03 if i%3==0 else -.01
            source.append({"history_id":str(i),"inst":"ABC-USDT","side":"LONG",
                "opened_ms":op,"closed_ms":op+60,"notional_usdt":5,
                "gross_pnl_usdt":net+.006,"fee_usdt":-.006,"net_pnl_usdt":net,
                "signal_snapshot":{"signal_close_ms":op-1,"rsi14":30+i,
                   "macd_hist":i*.001,"close":100+i,"sma10":99+i}})
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/"state.json"
            f.write_text(json.dumps({"trade_history":source}))
            rows,errors=load(f)
            self.assertEqual(len(rows),16)
            self.assertFalse(errors)
            frozen=rows[9]["closed_ms"]
            predicates,meta=freeze_bank(rows[:10],max_base=12)
            out=scan_bank(rows,predicates,frozen,depth=3)
            self.assertEqual(out["training_closed"],10)
            self.assertEqual(out["forward_closed"],6)
            self.assertGreater(out["variants_checked"],0)
            self.assertTrue(all(x["training"]["positions"]<=10 for x in out["top_frozen_training_filters"]))
            self.assertTrue(portfolio20(rows,(1<<len(rows))-1)["wins"]>=1)

    def test_fee_inconsistency_not_accepted(self):
        t={"opened_ms":1000,"closed_ms":1300,"signal_snapshot":{"signal_close_ms":999},
           "notional_usdt":5,"gross_pnl_usdt":1,"fee_usdt":-.1,"net_pnl_usdt":1}
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/"state.json"
            f.write_text(json.dumps({"trade_history":[t]}))
            rows,errors=load(f)
            self.assertFalse(rows)
            self.assertIn("inconsistent fees",errors)

if __name__=="__main__":
    unittest.main()
