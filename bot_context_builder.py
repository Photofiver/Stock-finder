"""Build a compact, durable, secret-free LIVE bot context for future analysis.

Run after the 5m LIVE scan, NEVER before placing orders. This does not call
BloFin or alter trading decisions, positions, orders, risk limits or state.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTEXT_FILE = Path(os.getenv("BOT_CONTEXT_FILE", str(ROOT / "bot_context.json")))
STATE_FILE = Path(os.getenv("LIVE_STATE_FILE", str(ROOT / "blofin_live_state.json")))
WORKFLOW_FILE = ROOT / ".github/workflows/blofin_live_signal_watch.yml"
SOURCE_FILES = (
    "blofin_ma10_ma20_live.py",
    "blofin_live_hourly.py",
    "blofin_live_signal_watch.py",
    "blofin_order_book.py",
    ".github/workflows/blofin_live_signal_watch.yml",
)


def date_utc(ms):
    if ms in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def live_env_from_workflow(path=WORKFLOW_FILE):
    """Only values in the real LIVE scan step, never secrets."""
    text = Path(path).read_text(encoding="utf-8")
    step_start = text.index("      - name: Scan 5m LIVE signal and execute AUTO after revalidation")
    text = text[step_start:]
    text = text[:text.index("        run: python blofin_live_signal_watch.py")]
    keys = (
        "LIVE_SIGNAL_MINUTES", "LIVE_UNIVERSE_TOP_N", "LIVE_ENTRY_TOP_N",
        "LIVE_MA_CROSS", "LIVE_SIGNAL_MAX_AGE_MS", "LIVE_MAX_BANKROLL_USDT",
        "LIVE_ENTRY_STRATEGY",
    )
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        for key in keys:
            if stripped.startswith(key + ":"):
                values[key] = stripped.partition(":")[2].strip().strip("'\"")
    missing = set(keys) - set(values)
    if missing:
        raise ValueError(f"Missing LIVE workflow parameters: {sorted(missing)}")
    return values


def file_hashes(root=ROOT):
    result = {}
    for relative_path in SOURCE_FILES:
        path = Path(root) / relative_path
        if path.is_file():
            result[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def trade_brief(trade):
    snapshot = trade.get("signal_snapshot") or {}
    order_book = snapshot.get("order_book") or {}
    return {
        "inst": trade.get("inst"),
        "side": trade.get("side"),
        "rank": trade.get("signal_rank"),
        "opened_utc": date_utc(trade.get("opened_ms")),
        "closed_utc": date_utc(trade.get("closed_ms")),
        "gross_pnl_usdt": trade.get("gross_pnl_usdt"),
        "net_pnl_usdt": trade.get("net_pnl_usdt"),
        "result": trade.get("result"),
        "exit_reason": trade.get("reason"),
        "order_book_directional_pct": order_book.get("directional_pct"),
        "order_book_confirmed": order_book.get("direction_confirmed"),
    }


def build_context(state, workflow_env, strategy, order_book, notes=None,
                  source_hashes=None, now=None):
    """State is read only. Previous manually researched notes are preserved."""
    notes = notes if isinstance(notes, list) else []
    history = state.get("trade_history") or []
    last = state.get("ma_cross_last_scan") or {}
    positions = state.get("positions") or {}
    position_summaries = [
        {
            "inst": p.get("inst", inst),
            "side": p.get("side"),
            "opened_utc": date_utc(p.get("opened_ms")),
            "hold_policy": p.get("hold_policy"),
        }
        for inst, p in positions.items()
        if isinstance(p, dict)
    ] if isinstance(positions, dict) else []
    event_list = last.get("events") or []
    rejected = [
        {
            "inst": e.get("inst"), "side": e.get("side"),
            "rank": e.get("rank"), "status": e.get("status"),
            "reason": str(e.get("reason") or e.get("error") or "")[:400],
        }
        for e in event_list if isinstance(e, dict)
    ]
    total = state.get("trades") or {}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include timezone")
    rank_cap = int(workflow_env["LIVE_ENTRY_TOP_N"])
    return {
        "schema_version": 1,
        "generated_utc": now.isoformat(),
        "description": "Current BloFin LIVE bot context. Read this file first before re-reading full code or trade history.",
        "provenance": {
            "repository": "Photofiver/Stock-finder",
            "workflow": ".github/workflows/blofin_live_signal_watch.yml",
            "main_strategy_file": "blofin_ma10_ma20_live.py",
            "source_sha256": source_hashes or {},
            "latest_live_run_utc": date_utc(state.get("last_run_ms")),
            "note": "Trading parameters reflect the live workflow and source code at generation. Last run may precede generation.",
        },
        "live_strategy": {
            "type": "SMA10_SMA20_CROSS",
            "candle_minutes": int(workflow_env["LIVE_SIGNAL_MINUTES"]),
            "rank_min": int(strategy.ENTRY_RANK_MIN),
            "rank_max": max(51, min(100, rank_cap)),
            "rank_scope": "ONLY CURRENT RANKS 51-100",
            "allowed_directions": ["LONG", "SHORT"],
            "rsi_long_min": 50.0,
            "rsi_long_max": float(strategy.RSI_ENTRY_MAX),
            "rsi_short_min": 0.0,
            "rsi_short_max": 50.0,
            "stochastic": {"periods": [14, 1, 3],
                           "long_rule": "K <= D", "short_rule": "K >= D"},
            "additional_entry_filters": [
                "MA10/MA20 CROSS at confirmed close",
                "long distance from live SMA10 max 1%",
                "support/resistance avoidance",
                "fresh BID/ASK and pre-order price checks",
                "TOP20 order book directional depth confirmation",
                "MACD(12,26,9) positive rising histogram for LONG; negative falling for SHORT",
            ],
            "order_book": {
                "indicator": "TOP20_ORDER_BOOK_DEPTH_IMBALANCE",
                "levels_per_side": order_book.DEPTH_LEVELS,
                "min_directional_share_pct": float(order_book.MIN_DIRECTIONAL_SHARE_PCT),
                "required_for_entry": True,
                "missing_or_stale_data": "BLOCK_ENTRY",
                "raw_snapshots": "blofin_order_books/YYYY-MM-DD.jsonl",
                "note": "Resting depth does not establish future price direction.",
            },
            "take_profit_pct": float(strategy.PRICE_TAKE_PROFIT_PCT) * 100,
            "stop_loss_initial_cap_pct": float(strategy.PRICE_STOP_LOSS_PCT) * 100,
            "stop_reference": "SMA10 ratchet, plus executable BID/ASK polling and broker TP/SL",
            "max_hold_minutes": strategy.MAX_HOLD_MS / 60000,
            "executable_exit_poll_seconds": strategy.EXIT_QUOTE_POLL_SECONDS,
            "max_bankroll_allocation_usdt": float(workflow_env["LIVE_MAX_BANKROLL_USDT"]),
            "leverage": "1x",
            "drawdown_stop_pct": 10.0,
            "strategy_family": workflow_env["LIVE_ENTRY_STRATEGY"],
            "workflow_enabled": workflow_env["LIVE_MA_CROSS"].lower() == "true",
        },
        "last_known_state": {
            "last_live_scan_utc": date_utc(state.get("last_scan_close_ms")),
            "last_run_utc": date_utc(state.get("last_run_ms")),
            "last_scan_status": last.get("status"),
            "risk_stop_triggered": bool(state.get("risk_stop_triggered")),
            "open_positions": position_summaries,
            "trade_history_records": len(history),
            "lifetime_counters": {
                "total": total.get("total"), "wins": total.get("wins"),
                "losses": total.get("losses"), "flat": total.get("flat"),
                "unverified": total.get("unverified"),
            },
            "gross_realized_pnl_usdt": state.get("gross_realized_pnl_usdt"),
            "fees_usdt": state.get("fees_usdt"),
            "net_realized_pnl_usdt": state.get("realized_pnl_usdt"),
            "bankroll_start_usdt": state.get("bankroll_start_usdt"),
            "current_bankroll_estimated_usdt": _current_bankroll(state),
            "last_scan_rejections": rejected[-12:],
            "last_12_trades": [trade_brief(t) for t in history[-12:]],
        },
        "persistent_analysis_notes": notes,
        "source_files": {
            "full_trade_history": "blofin_live_state.json",
            "detailed_order_books": "blofin_order_books/",
            "scan_audits": "blofin_live_scans/",
            "generated_trade_charts": "blofin_trade_charts/",
            "tests": "test_blofin_ma_no_adx.py, test_blofin_ma_rsi75.py, test_blofin_order_book.py",
        },
        "instructions_for_future_analysis": [
            "Read bot_context.json first; use SHA256 checks to tell if code has changed.",
            "Only re-read source files if code hash or task scope requires it.",
            "Use full trade_history for exact counts; lifetime counters can differ due to unverified closes.",
            "Do not treat order book imbalance as a historically validated predictor without an out-of-sample comparison.",
            "Add explicitly verified research findings to persistent_analysis_notes; the updater preserves them.",
            "Never store API credentials, access tokens, or personal notification endpoints here.",
        ],
    }


def _current_bankroll(state):
    try:
        from decimal import Decimal
        start = Decimal(str(state.get("bankroll_start_usdt") or 0))
        pnl = Decimal(str(state.get("realized_pnl_usdt") or 0))
        baseline = Decimal(str(state.get("bankroll_pnl_baseline_usdt") or 0))
        return str(max(Decimal(0), start + pnl - baseline))
    except (TypeError, ValueError, ArithmeticError):
        return None


def update_context():
    import blofin_ma10_ma20_live as strategy
    import blofin_order_book as order_book

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("Invalid live state data")
    previous = {}
    if CONTEXT_FILE.exists():
        previous = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            raise ValueError("Invalid previous bot_context.json")
    output = build_context(
        state, live_env_from_workflow(), strategy, order_book,
        notes=previous.get("persistent_analysis_notes", []),
        source_hashes=file_hashes(),
    )
    temp = CONTEXT_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    temp.replace(CONTEXT_FILE)
    print("BOT_CONTEXT_UPDATED",
          output["last_known_state"]["last_run_utc"],
          output["last_known_state"]["last_scan_status"])


if __name__ == "__main__":
    update_context()
