"""Read-only shadow audit of rejected LIVE signals; never submits an order."""
import json
from datetime import datetime, timezone
from pathlib import Path

import blofin_live_hourly as bot

AUDIT_DIR = Path("blofin_live_scans")
FEE_PCT_ASSUMED = 0.12  # Analysis only; not an actual broker fee record.
MAX_REVIEWS_PER_RUN = 2


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def outcome_for_side(side, entry, candle):
    if not entry or entry <= 0:
        return {"status": "NO_ENTRY_PRICE"}
    high, low, close = (float(candle[key]) for key in ("h", "l", "c"))
    favorable = (high / entry - 1) * 100 if side == "LONG" else (1 - low / entry) * 100
    adverse = (1 - low / entry) * 100 if side == "LONG" else (high / entry - 1) * 100
    directional_close = (close / entry - 1) * 100 if side == "LONG" else (1 - close / entry) * 100
    result = {
        "direction": side,
        "favorable_pct": favorable,
        "adverse_pct": adverse,
        "directional_close_pct": directional_close,
        "estimated_next_close_net_pct_after_assumed_fees": directional_close - FEE_PCT_ASSUMED,
    }
    for threshold in (0.5, 1.0):
        hit_tp = favorable >= threshold
        hit_sl = adverse >= threshold
        label = str(threshold).replace(".", "_")
        result["tp_" + label + "_hit"] = hit_tp
        result["sl_" + label + "_hit"] = hit_sl
        result["outcome_" + label] = (
            "BOTH_ORDER_UNKNOWN" if hit_tp and hit_sl else
            "TP_HIT" if hit_tp else "SL_HIT" if hit_sl else "NEITHER"
        )
    return result


def review_prior_scans():
    if not AUDIT_DIR.is_dir():
        return
    cache = {}
    processed = 0
    for path in sorted(AUDIT_DIR.glob("live_watch_*.json"), reverse=True):
        if processed >= MAX_REVIEWS_PER_RUN:
            break
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            print(f"AUDIT_READ_ERROR {path}: {exc}")
            continue
        if payload.get("next_candle_review_complete"):
            continue
        target_close_ms = int(payload.get("signal_close_ms") or 0) + bot.SIGNAL_MS
        if bot.now_ms() < target_close_ms:
            continue
        processed += 1
        for row in payload.get("instruments", []):
            if row.get("next_candle_review", {}).get("status") in ("DONE", "NO_BASE", "EXPIRED"):
                continue
            inst = row.get("inst")
            entry = (row.get("candle") or {}).get("close")
            if not inst or not entry:
                row["next_candle_review"] = {"status": "NO_BASE"}
                continue
            if inst not in cache:
                try:
                    cache[inst] = bot.fetch_signal_bars(inst)
                except Exception as exc:
                    cache[inst] = exc
            bars = cache[inst]
            if isinstance(bars, Exception):
                row["next_candle_review"] = {"status": "ERROR", "error": str(bars)}
                continue
            nxt = next((bar for bar in bars if bot.bar_close_ms(bar) == target_close_ms), None)
            if nxt is None:
                oldest = bot.bar_close_ms(bars[0]) if bars else 0
                row["next_candle_review"] = {
                    "status": "EXPIRED" if oldest > target_close_ms else "PENDING",
                    "target_close_ms": target_close_ms,
                }
                continue
            row["next_candle_review"] = {
                "status": "DONE",
                "next_candle_close_ms": bot.bar_close_ms(nxt),
                "open": float(nxt["o"]), "high": float(nxt["h"]),
                "low": float(nxt["l"]), "close": float(nxt["c"]),
                "LONG": outcome_for_side("LONG", float(entry), nxt),
                "SHORT": outcome_for_side("SHORT", float(entry), nxt),
            }
        payload["next_candle_review_complete"] = all(
            row.get("next_candle_review", {}).get("status") in ("DONE", "NO_BASE", "EXPIRED")
            for row in payload.get("instruments", [])
        )
        payload["reviewed_at_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(path, payload)
        done = sum(row.get("next_candle_review", {}).get("status") == "DONE" for row in payload.get("instruments", []))
        print(f"AUDIT_REVIEW {path.name} reviewed={done} complete={payload['next_candle_review_complete']}")


def record_current_scan():
    state = bot.load_state()
    diagnostic = state.get("last_diagnostic") or {}
    signal_close_ms = int(state.get("last_scan_close_ms") or 0)
    records = diagnostic.get("instruments") or []
    if not signal_close_ms or not records:
        print("AUDIT_NO_NEW_DIAGNOSTIC")
        return
    stamp = datetime.fromtimestamp(signal_close_ms / 1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = AUDIT_DIR / f"live_watch_{stamp}.json"
    if path.exists():
        print(f"AUDIT_ALREADY_SAVED {path.name}")
        return
    instruments = []
    for item in records:
        candle = item.get("candle") or {}
        sides = {}
        for side in ("LONG", "SHORT"):
            metrics = item.get(side.lower()) or {}
            if not metrics:
                continue
            missing = list(metrics.get("missing") or [])
            sides[side] = {
                "score": metrics.get("score"),
                "missing": missing,
                "would_pass_candle_filters": not missing,
                "metrics": metrics,
            }
        instruments.append({
            "inst": item.get("inst"),
            "rank": item.get("rank"),
            "signal_close_ms": item.get("close_ms"),
            "candle": candle,
            "rsi14": item.get("rsi14"),
            "macd": item.get("macd"),
            "chart_patterns": item.get("chart_patterns"),
            "sides": sides,
            "data_error": item.get("error"),
            "next_candle_review": {"status": "PENDING" if candle.get("close") else "NO_BASE"},
        })
    payload = {
        "format_version": 2,
        "source": "BloFin LIVE signal watch; read-only retrospective audit",
        "signal_close_ms": signal_close_ms,
        "signal_close_utc": datetime.fromtimestamp(signal_close_ms / 1000, timezone.utc).isoformat(),
        "diagnostic_generated_at_ms": diagnostic.get("generated_at_ms"),
        "strategy_result": diagnostic.get("result"),
        "top7": state.get("last_top7") or diagnostic.get("top7"),
        "analysis_only": True,
        "hypothetical_entry_price": "signal candle close; excludes spread and slippage",
        "assumed_roundtrip_fees_pct": FEE_PCT_ASSUMED,
        "price_thresholds_tested_pct": [0.5, 1.0],
        "limitations": "Next candle OHLC cannot establish TP/SL order if both touch; not a simulation of the complete LIVE exit strategy.",
        "instruments": instruments,
        "next_candle_review_complete": False,
    }
    save_json(path, payload)
    print(f"AUDIT_SAVED {path.name} instruments={len(instruments)}")


def main():
    # Separate process after LIVE state was safely saved; public market data only.
    review_prior_scans()
    record_current_scan()


if __name__ == "__main__":
    main()
