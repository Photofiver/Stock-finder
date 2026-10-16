"""Offline regression tests for auto-saved LIVE bot memory (no exchange calls)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot_context_builder as memory


def fake_state():
    return {
        "last_run_ms": 1792135607596,
        "last_scan_close_ms": 1792135500000,
        "risk_stop_triggered": False,
        "positions": {"Q-USDT": {
            "inst": "Q-USDT", "side": "LONG", "opened_ms": 1792135500000,
            "hold_policy": "SMA10_DYNAMIC_BROKER_STOP",
        }},
        "trades": {"total": 3, "wins": 2, "losses": 1, "flat": 0, "unverified": 0},
        "trade_history": [
            {"inst": "Q-USDT", "side": "LONG", "opened_ms": 1792135500000,
             "closed_ms": 1792135600000, "gross_pnl_usdt": 0.1,
             "net_pnl_usdt": 0.08, "result": "WIN", "signal_rank": 51,
             "signal_snapshot": {"order_book": {
                 "directional_pct": 62.8, "direction_confirmed": True}},
            "API_KEY": "DO_NOT_SAVE"},
        ],
        "ma_cross_last_scan": {
            "status": "OPENED", "events": [
                {"inst": "A-USDT", "side": "SHORT", "rank": 53,
                 "status": "ORDER_BOOK_DIRECTION_BLOCKED", "reason": "depth <55%"}, 
            ],
        },
        "bankroll_start_usdt": "10",
        "realized_pnl_usdt": "1.5",
        "bankroll_pnl_baseline_usdt": "0.5",
        "BLOFIN_SECRET_KEY": "DO_NOT_SAVE",
    }


def strategy():
    return SimpleNamespace(
        ENTRY_RANK_MIN=51, RSI_ENTRY_MAX=75,
        PRICE_TAKE_PROFIT_PCT=0.01, PRICE_STOP_LOSS_PCT=0.0025,
        MAX_HOLD_MS=600000, EXIT_QUOTE_POLL_SECONDS=1.0,
    )


def depth():
    return SimpleNamespace(DEPTH_LEVELS=20, MIN_DIRECTIONAL_SHARE_PCT=55)


class BotContextTests(unittest.TestCase):
    def test_reads_real_workflow_configuration_and_no_secrets(self):
        env = memory.live_env_from_workflow()
        self.assertEqual(env["LIVE_SIGNAL_MINUTES"], "5")
        self.assertEqual(env["LIVE_ENTRY_TOP_N"], "100")
        self.assertEqual(env["LIVE_MA_CROSS"], "true")
        self.assertNotIn("BLOFIN_API_KEY", env)
        self.assertNotIn("BLOFIN_SECRET_KEY", env)

    def test_build_preserves_notes_and_operational_summary(self):
        env = memory.live_env_from_workflow()
        ctx = memory.build_context(
            fake_state(), env, strategy(), depth(),
            notes=[{"finding": "Previously checked", "sample": 100}],
            source_hashes={"test.py": "abc"},
            now=datetime(2026, 10, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(ctx["live_strategy"]["rank_min"], 51)
        self.assertEqual(ctx["live_strategy"]["rank_max"], 100)
        self.assertEqual(ctx["live_strategy"]["rsi_long_min"], 50)
        self.assertEqual(ctx["live_strategy"]["rsi_short_max"], 50)
        self.assertEqual(ctx["live_strategy"]["take_profit_pct"], 1.0)
        self.assertEqual(ctx["live_strategy"]["stop_loss_initial_cap_pct"], 0.25)
        self.assertEqual(ctx["live_strategy"]["order_book"]["levels_per_side"], 20)
        self.assertEqual(ctx["last_known_state"]["current_bankroll_estimated_usdt"], "11.0")
        self.assertEqual(ctx["last_known_state"]["last_scan_status"], "OPENED")
        self.assertEqual(ctx["persistent_analysis_notes"][0]["sample"], 100)
        self.assertEqual(
            ctx["last_known_state"]["last_12_trades"][0]["order_book_directional_pct"],
            62.8,
        )
        text = json.dumps(ctx)
        self.assertNotIn("DO_NOT_SAVE", text)
        self.assertNotIn("BLOFIN_SECRET_KEY", text)

    def test_update_retains_previous_analysis_notes_across_runs(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            context_path = Path(td) / "context.json"
            state_path.write_text(json.dumps(fake_state()), encoding="utf-8")
            notes = [{"finding": "Trial", "checked": True}]
            context_path.write_text(json.dumps({
                "persistent_analysis_notes": notes
            }), encoding="utf-8")
            with patch.object(memory, "STATE_FILE", state_path), \
                 patch.object(memory, "CONTEXT_FILE", context_path):
                memory.update_context()
                first = json.loads(context_path.read_text(encoding="utf-8"))
                self.assertEqual(first["persistent_analysis_notes"], notes)
                self.assertEqual(first["last_known_state"]["trade_history_records"], 1)
                memory.update_context()
                second = json.loads(context_path.read_text(encoding="utf-8"))
                self.assertEqual(second["persistent_analysis_notes"], notes)

    def test_no_partial_context_on_bad_state_json(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            context_path = Path(td) / "context.json"
            context_path.write_text('{"keep": true}', encoding="utf-8")
            state_path.write_text('BAD JSON', encoding="utf-8")
            with patch.object(memory, "STATE_FILE", state_path), \
                 patch.object(memory, "CONTEXT_FILE", context_path):
                with self.assertRaises(json.JSONDecodeError):
                    memory.update_context()
                self.assertEqual(
                    json.loads(context_path.read_text(encoding="utf-8")),
                    {"keep": True},
                )


if __name__ == "__main__":
    unittest.main()
