"""Fast append-only LIVE scan summary, kept away from slow research archives.

READS the existing saved JSON state, WRITES only a small per-day JSONL file.
No network access, no BloFin API calls, no trades, no secrets and no indicators
calculated after the signal. Run after the LIVE scanner and before committing
state, independently of the slow historical-audit workflow.
"""
import gzip
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


def safe_rsi89_event(state):
    """Only archive the exact confirmed scan just saved by LIVE."""
    if not isinstance(state, dict):
        return None
    scan = state.get("volume_long_last_scan")
    if not isinstance(scan, dict):
        return None
    audit = scan.get("rsi89_audit")
    close_ms = int(scan.get("signal_close_ms") or 0)
    if (not isinstance(audit, dict) or close_ms <= 0
            or close_ms != int(state.get("last_scan_close_ms") or 0)
            or close_ms != int(audit.get("signal_close_ms") or 0)):
        return None
    return {
        "source": "BLOFIN_LIVE_RSI89_TOP200_AUDIT",
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "strategy": scan.get("strategy"), "signal_close_ms": close_ms,
        "status": scan.get("status"),
        "scanned_confirmed": scan.get("scanned_confirmed"),
        "matched": scan.get("matched"), "entries": scan.get("entries", []),
        "exits": scan.get("exits", []),
        "sl_updates": scan.get("sl_updates", []),
        "rejections": scan.get("rejections", []),
        "candidates": scan.get("candidates", []),
        "audit": audit,
    }


def _last_jsonl_row(path, compressed=False):
    if not path.exists():
        return None
    opener = gzip.open if compressed else open
    last = None
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last = json.loads(line)
    return last


def append_rsi89_event(state_path=STATE_FILE, events_dir=EVENTS_DIR):
    """Store full TOP200 observations compressed plus small searchable JSONL summaries."""
    state_path, events_dir = Path(state_path), Path(events_dir)
    if not state_path.exists():
        print("RSI89_AUDIT_SKIP no LIVE state")
        return None
    event = safe_rsi89_event(json.loads(state_path.read_text(encoding="utf-8")))
    if event is None:
        print("RSI89_AUDIT_SKIP no new matching scan")
        return None
    close_ms = event["signal_close_ms"]
    close = datetime.fromtimestamp(close_ms / 1000, timezone.utc)
    events_dir.mkdir(parents=True, exist_ok=True)
    archive = events_dir / f"rsi89_full_5m_{close:%Y%m%d}.jsonl.gz"
    summary = events_dir / f"rsi89_nearest_5m_{close:%Y%m%d}.jsonl"

    if (_last_jsonl_row(archive, compressed=True) or {}).get("signal_close_ms") != close_ms:
        # Appending gzip members preserves full readable JSONL with minimal repo growth.
        with gzip.open(archive, "at", encoding="utf-8", compresslevel=6) as fp:
            fp.write(json.dumps(event, ensure_ascii=False,
                                separators=(",", ":"), allow_nan=False) + "\n")
    audit = event["audit"]
    compact = {
        "source": event["source"], "recorded_utc": event["recorded_utc"],
        "strategy": event["strategy"], "signal_close_ms": close_ms,
        "status": event["status"], "scanned_confirmed": event["scanned_confirmed"],
        "matched": event["matched"], "entries": event["entries"],
        "rejections": event["rejections"], "rsi_available": audit.get("rsi_available_count"),
        "reason_counts": audit.get("reason_counts", {}),
        "closest_rsi89": audit.get("closest_rsi89", []),
        "nearest_approaching_from_below": audit.get("nearest_approaching_from_below", []),
        "audit_error": audit.get("error"),
        "full_archive": archive.name,
    }
    if (_last_jsonl_row(summary) or {}).get("signal_close_ms") != close_ms:
        with summary.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(compact, ensure_ascii=False,
                                separators=(",", ":"), allow_nan=False) + "\n")
    print(f"RSI89_AUDIT_SAVED {archive} + {summary} scan={close_ms}"
          f" observed={audit.get('rsi_available_count', 0)}/200"
          f" closest={len(audit.get('closest_rsi89', []))}")
    return archive



if __name__ == "__main__":
    failed = False
    for writer in (append_fast_event, append_rsi89_event):
        try:
            writer()
        except Exception as exc:
            failed = True
            print(f"{writer.__name__}_ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
    if failed:
        sys.exit(1)
