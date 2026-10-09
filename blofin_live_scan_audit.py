"""Read-only BloFin diagnostics in one continually updated JSON file.

This script does not place orders, modify the LIVE bot, or read private API keys.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import blofin_live_hourly as bot

HISTORY_FILE = Path("blofin_live_scans/live_watch_history.json")
FEE_PCT_ASSUMED = 0.12
MAX_SCANS = 5000  # ~52 days at 15-minute intervals; bound GitHub file size.
MAX_REVIEWS_PER_RUN = 2


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def load_history():
    if not HISTORY_FILE.exists():
        return {
            "format_version": 3,
            "source": "BloFin LIVE signal watch; read-only retrospective audit",
            "analysis_only": True,
            "signal_minutes": bot.SIGNAL_MINUTES,
            "assumed_roundtrip_fees_pct": FEE_PCT_ASSUMED,
            "price_thresholds_tested_pct": [0.5, 1.0],
            "indicator_definitions": {
                "adx14": "Wilder ADX(14), 15m CLOSED candle",
                "obv_rising_5": "OBV close minus OBV 5 bars earlier > 0; 15m CLOSED candles",
            },
            "notes": (
                "Hypothetical entry at signal candle close; no spread or slippage. "
                "When a candle hits both TP and SL, first-hit order is unknown."
            ),
            "scans": [],
            "dropped_oldest_scans": 0,
        }
    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    if not isinstance(history, dict) or not isinstance(history.get("scans"), list):
        raise ValueError("Invalid existing audit history; refusing to overwrite it")
    return history


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


def adx14_and_obv5(bars):
    """Wilder smoothing of TR/+DM/-DM and five-bar OBV trend, closed bars only."""
    period = 14
    if len(bars) < period * 2 + 1:
        return None, None
    tr, pos, neg = [], [], []
    obv = [0.0]
    for i in range(1, len(bars)):
        previous, current = bars[i - 1], bars[i]
        ph, pl, pc = (float(previous[k]) for k in ("h", "l", "c"))
        h, l, c, volume = (float(current[k]) for k in ("h", "l", "c", "v"))
        up, down = h - ph, pl - l
        pos.append(up if up > down and up > 0 else 0.0)
        neg.append(down if down > up and down > 0 else 0.0)
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        obv.append(obv[-1] + (volume if c > pc else -volume if c < pc else 0.0))
    tr_sum = sum(tr[:period])
    pos_sum = sum(pos[:period])
    neg_sum = sum(neg[:period])
    dx_values = []
    for i in range(period - 1, len(tr)):
        if i >= period:
            tr_sum = tr_sum - tr_sum / period + tr[i]
            pos_sum = pos_sum - pos_sum / period + pos[i]
            neg_sum = neg_sum - neg_sum / period + neg[i]
        plus_di = 100 * pos_sum / tr_sum if tr_sum else 0.0
        minus_di = 100 * neg_sum / tr_sum if tr_sum else 0.0
        denominator = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
    if len(dx_values) < period:
        return None, None
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    obv_delta_5 = obv[-1] - obv[-6]
    return adx, obv_delta_5


def fetch_cached(inst, cache):
    if inst not in cache:
        try:
            cache[inst] = bot.fetch_signal_bars(inst)
        except Exception as exc:
            print(f"AUDIT_MARKET_ERROR {inst} {type(exc).__name__}: {exc}")
            cache[inst] = exc
    return cache[inst]


def review_previous(history, cache):
    reviewed = 0
    changed = False
    for scan in reversed(history["scans"]):
        if reviewed >= MAX_REVIEWS_PER_RUN:
            break
        if scan.get("next_candle_review_complete"):
            continue
        target_close_ms = int(scan["signal_close_ms"]) + bot.SIGNAL_MS
        if bot.now_ms() < target_close_ms:
            continue
        reviewed += 1
        for row in scan.get("instruments", []):
            status = row.get("next_candle_review", {}).get("status")
            if status in ("DONE", "NO_BASE", "EXPIRED"):
                continue
            inst = row.get("inst")
            entry = (row.get("candle") or {}).get("close")
            if not inst or not entry:
                row["next_candle_review"] = {"status": "NO_BASE"}
                changed = True
                continue
            bars = fetch_cached(inst, cache)
            if isinstance(bars, Exception):
                row["next_candle_review"] = {"status": "ERROR", "error": str(bars)}
                changed = True
                continue
            following = next(
                (bar for bar in bars if bot.bar_close_ms(bar) == target_close_ms),
                None,
            )
            if following is None:
                oldest_close = bot.bar_close_ms(bars[0]) if bars else 0
                row["next_candle_review"] = {
                    "status": "EXPIRED" if oldest_close > target_close_ms else "PENDING",
                    "target_close_ms": target_close_ms,
                }
                changed = True
                continue
            row["next_candle_review"] = {
                "status": "DONE",
                "next_candle_close_ms": target_close_ms,
                "open": float(following["o"]),
                "high": float(following["h"]),
                "low": float(following["l"]),
                "close": float(following["c"]),
                "LONG": outcome_for_side("LONG", float(entry), following),
                "SHORT": outcome_for_side("SHORT", float(entry), following),
            }
            changed = True
        scan["next_candle_review_complete"] = all(
            row.get("next_candle_review", {}).get("status") in ("DONE", "NO_BASE", "EXPIRED")
            for row in scan.get("instruments", [])
        )
        scan["reviewed_at_utc"] = utc_now()
        changed = True
        done = sum(
            row.get("next_candle_review", {}).get("status") == "DONE"
            for row in scan.get("instruments", [])
        )
        print(f"AUDIT_REVIEW scan={scan['signal_close_ms']} done={done} complete={scan['next_candle_review_complete']}")
    return changed


def record_current_scan(history, cache):
    state = bot.load_state()
    diagnostic = state.get("last_diagnostic") or {}
    signal_close_ms = int(state.get("last_scan_close_ms") or 0)
    items = diagnostic.get("instruments") or []
    if not signal_close_ms or not items:
        print("AUDIT_NO_NEW_DIAGNOSTIC")
        return False
    if any(int(s.get("signal_close_ms") or 0) == signal_close_ms for s in history["scans"]):
        print(f"AUDIT_ALREADY_SAVED scan={signal_close_ms}")
        return False

    instruments = []
    for item in items:
        candle = item.get("candle") or {}
        inst = item.get("inst")
        adx14, obv_delta_5 = None, None
        indicator_error = None
        if inst and candle.get("close"):
            bars = fetch_cached(inst, cache)
            if isinstance(bars, Exception):
                indicator_error = str(bars)
            else:
                closed_bars = [bar for bar in bars if bot.bar_close_ms(bar) <= signal_close_ms]
                if closed_bars and bot.bar_close_ms(closed_bars[-1]) == signal_close_ms:
                    adx14, obv_delta_5 = adx14_and_obv5(closed_bars)
                else:
                    indicator_error = "Signal candle not in available closed-bar history"
                    print(f"AUDIT_CANDLE_MISSING {inst} {signal_close_ms}")
        instruments.append({
            "inst": inst,
            "rank": item.get("rank"),
            "signal_close_ms": item.get("close_ms"),
            "candle": candle,
            "rsi14": item.get("rsi14"),
            "adx14": adx14,
            "obv_delta_5": obv_delta_5,
            "obv_rising_5": (obv_delta_5 > 0) if obv_delta_5 is not None else None,
            "short_adx40_obv5": (
                adx14 >= 40 and obv_delta_5 > 0
                if adx14 is not None and obv_delta_5 is not None else None
            ),
            "macd": item.get("macd"),
            "chart_patterns": item.get("chart_patterns"),
            "short": item.get("short"),
            "long": item.get("long"),
            "data_error": item.get("error") or indicator_error,
            "next_candle_review": {"status": "PENDING" if candle.get("close") else "NO_BASE"},
        })

    scan = {
        "signal_close_ms": signal_close_ms,
        "signal_close_utc": datetime.fromtimestamp(signal_close_ms / 1000, timezone.utc).isoformat(),
        "captured_utc": utc_now(),
        "diagnostic_generated_at_ms": diagnostic.get("generated_at_ms"),
        "strategy_result": diagnostic.get("result"),
        "top7": state.get("last_top7") or diagnostic.get("top7"),
        "instruments": instruments,
        "next_candle_review_complete": False,
    }
    history["scans"].append(scan)
    if len(history["scans"]) > MAX_SCANS:
        dropped = len(history["scans"]) - MAX_SCANS
        del history["scans"][:dropped]
        history["dropped_oldest_scans"] = int(history.get("dropped_oldest_scans") or 0) + dropped
    print(f"AUDIT_SAVED scan={signal_close_ms} instruments={len(instruments)} total={len(history['scans'])}")
    return True


def main():
    history = load_history()
    cache = {}
    updated = review_previous(history, cache)
    updated = record_current_scan(history, cache) or updated
    if updated or not HISTORY_FILE.exists():
        history["updated_at_utc"] = utc_now()
        history["scan_count"] = len(history["scans"])
        save_json(HISTORY_FILE, history)
        print(f"AUDIT_HISTORY_UPDATED path={HISTORY_FILE} scans={history['scan_count']}")
    else:
        print("AUDIT_HISTORY_UNCHANGED")


if __name__ == "__main__":
    main()
