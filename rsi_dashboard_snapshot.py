#!/usr/bin/env python3
"""Public dashboard projection of LIVE state. Read-only: never writes bot files."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

TRADE_FIELDS = (
    "inst", "side", "opened_ms", "closed_ms", "net_pnl_usdt",
    "notional_usdt", "reason", "exit_rsi14", "rsi_exit",
    "rsi_min", "rsi14_min", "rsi_max", "rsi14_max", "entry_rsi14",
    "strategy", "result",
)
POSITION_FIELDS = ("inst", "side", "opened_ms", "notional_usdt", "tp", "sl", "protection_status")
SIGNAL_FIELDS = ("rsi14", "rsi", "rsi89_short_entry")
ROOT_FIELDS = (
    "realized_pnl_usdt", "bankroll_pnl_baseline_usdt", "bankroll_start_usdt",
    "bankroll_streak_bonus_usdt", "last_run_ms", "last_scan_close_ms",
)


def compact(record, fields):
    return {key: record[key] for key in fields if key in record}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    if not isinstance(state.get("trade_history"), list) or not isinstance(state.get("trades"), dict):
        raise ValueError("LIVE state is incomplete; not publishing an empty dashboard")
    history = []
    for record in state["trade_history"]:
        if not isinstance(record, dict):
            continue
        trade = compact(record, TRADE_FIELDS)
        signal = record.get("signal_snapshot")
        if isinstance(signal, dict):
            trade["signal_snapshot"] = compact(signal, SIGNAL_FIELDS)
        history.append(trade)
    source_positions = state.get("positions") or {}
    positions = {}
    if isinstance(source_positions, dict):
        for key, record in source_positions.items():
            if not isinstance(record, dict):
                continue
            pos = compact(record, POSITION_FIELDS)
            signal = record.get("signal_snapshot")
            if isinstance(signal, dict):
                pos["signal_snapshot"] = compact(signal, SIGNAL_FIELDS)
            positions[str(key)] = pos
    last_scan = state.get("volume_long_last_scan") or {}
    if not isinstance(last_scan, dict):
        last_scan = {}
    rsi_audit = last_scan.get("rsi89_audit")
    if not isinstance(rsi_audit, dict):
        rsi_audit = {}
    candidates = rsi_audit.get("nearest_approaching_from_below")
    rsi_audit = {
        "nearest_approaching_from_below": [
            compact(r, ("inst", "rank", "reason", "rsi14", "gap_to_89_points"))
            for r in (candidates if isinstance(candidates, list) else [])[:8]
            if isinstance(r, dict)
        ]
    }
    payload = {
        "snapshot_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "trade_history": history,
        "positions": positions,
        "trades": compact(state["trades"], ("wins", "losses", "flat", "total")),
        "volume_long_last_scan": {"status": last_scan.get("status"), "rsi89_audit": rsi_audit},
    }
    payload.update(compact(state, ROOT_FIELDS))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    print(f"Dashboard snapshot: {len(history)} closed, {len(positions)} open, {out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
