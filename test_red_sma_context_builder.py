"""No-network RED strategy context test for the async archive updater."""
import json
import tempfile
import unittest
from pathlib import Path
import bot_context_builder as context

class ContextTests(unittest.TestCase):
    def test_current_red_strategy_is_reported_not_legacy_sma(self):
        state={
            "last_scan_close_ms":1792302000000,
            "last_run_ms":1792302010000,
            "last_top200":["BTC-USDT","ETH-USDT"],
            "red_sma_last_scan":{
                "strategy":"RED_02_06_THEN_RED_04_08_SMA10_MINUS2_LONG_V1",
                "signal_close_ms":1792302000000,
                "status":"NO_MATCH","scanned_confirmed":200,"matched":0,
            },
            "positions":{},"trades":{"total":0,"wins":0,"losses":0},
            "trade_history":[],
        }
        original_state,original_output=context.STATE_FILE,context.CONTEXT_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                context.STATE_FILE=Path(td)/"live.json"
                context.CONTEXT_FILE=Path(td)/"context.json"
                context.STATE_FILE.write_text(json.dumps(state),encoding="utf-8")
                context.update_context()
                doc=json.loads(context.CONTEXT_FILE.read_text())
                self.assertEqual(doc["provenance"]["main_strategy_file"],
                                 "blofin_red_sma_rebound_live.py")
                self.assertEqual(doc["live_strategy"]["type"],
                                 "TWO_RED_CANDLES_OVERSOLD_SMA10_REBOUND")
                self.assertEqual(doc["live_strategy"]["take_profit_pct"],1.5)
                self.assertEqual(doc["live_strategy"]["stop_loss_pct"],1.0)
                self.assertEqual(doc["last_known_state"]["last_scan_confirmed_count"],200)
                self.assertEqual(doc["last_known_state"]["last_scan_status"],"NO_MATCH")
                self.assertEqual(doc["source_files"]["fast_scan_audits"],
                                 "blofin_fast_scan_events/")
        finally:
            context.STATE_FILE=original_state
            context.CONTEXT_FILE=original_output

if __name__=="__main__":
    unittest.main()
