"""Fast append-only LIVE scan summary, kept away from slow research archives.

READS the existing saved JSON state, WRITES only a small per-day JSONL file.
No network access, no BloFin API calls, no trades, no secrets and no indicators
calculated after the signal. Run after the LIVE scanner and before committing
state, independently of the slow historical-audit workflow.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("blofin_live_state.json")
EVENTS_DIR = Path("blofin_fast_scan_events")
STRATEGY_PREFIX = "RED_02_06_THEN_RED_04_08_SMA10_MINUS2_LONG_"


def safe_scan_event(state):
    if not isinstance(state, dict):
        return None
    scan = state.get("red_sma_last_scan")
    if not isinstance(scan, dict):
        return None
    close_ms = int(scan.get("signal_close_ms") or 0)
    if (close_ms <= 0
            or close_ms != int(state.get("last_scan_close_ms") or 0)
            or not str(scan.get("strategy") or "").startswith(STRATEGY_PREFIX)):
        return None
    # Keep the full actual ranked TOP200 of THIS scan (point-in-time ticker
    # order), counts and matching candidates. Sensitive broker state and all
    # secrets are deliberately excluded.
    keys = (
        "strategy", "signal_close_ms", "rank_basis", "status",
        "top200_count", "scanned_confirmed", "matched", "candidates",
        "selected", "rejections", "positions", "error",
    )
    event = {key: scan[key] for key in keys if key in scan}
    event["top200_at_scan"] = list(state.get("last_top200") or [])[:200]
    event["recorded_utc"] = datetime.now(timezone.utc).isoformat()
    event["source"] = "BLOFIN_LIVE_REBOUND_SCAN"
    return event


def append_fast_event(state_path=STATE_FILE, events_dir=EVENTS_DIR):
    state_path = Path(state_path)
    events_dir = Path(events_dir)
    if not state_path.exists():
        print("FAST_SCAN_LOG_SKIP no live state file")
        return None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    record = safe_scan_event(state)
    if not record:
        print("FAST_SCAN_LOG_SKIP not a new 5m red-SMA live scan")
        return None
    close = datetime.fromtimestamp(record["signal_close_ms"]/1000, timezone.utc)
    target = events_dir/f"red_sma_5m_{close:%Y%m%d}.jsonl"
    events_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()
        if lines:
            latest = json.loads(lines[-1])
            if int(latest.get("signal_close_ms") or 0) == record["signal_close_ms"]:
                print(f"FAST_SCAN_LOG_DUPLICATE {record['signal_close_ms']}")
                return target
    with target.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False, separators=(",",":"))+"\n")
    print(f"FAST_SCAN_LOG_SAVED {target} scan={record['signal_close_ms']}"
          f" confirmed={record.get('scanned_confirmed')} matches={record.get('matched')}")
    return target


if __name__ == "__main__":
    try:
        append_fast_event()
    except Exception as exc:
        print(f"FAST_SCAN_LOG_ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
