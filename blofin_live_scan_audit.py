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
            "format_version": 4,
            "source": "BloFin LIVE signal watch; read-only retrospective audit",
            "analysis_only": True,
            "signal_minutes": bot.SIGNAL_MINUTES,
            "assumed_roundtrip_fees_pct": FEE_PCT_ASSUMED,
            "price_thresholds_tested_pct": [0.5, 1.0],
            "indicator_definitions": {
                "adx14": "Wilder ADX(14), 15m CLOSED candle",
                "obv_rising_5": "OBV close minus OBV 5 bars earlier > 0; 15m CLOSED candles",
                "indicator_change_over_15m": "after next closed 15m bar minus value at entry signal",
                "best_coin_comparison_after_15m": "ex-post only, 7 assets x 2 hypothetical directions",
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
    history["format_version"] = 4
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


def snapshot_indicators(bars):
    """Values calculated using only candles closed at the snapshot time."""
    if not bars:
        return None
    cur = bars[-1]
    adx, obv5 = adx14_and_obv5(bars)
    close = float(cur["c"])
    hist = cur.get("macd_hist")
    long_metrics = bot.long_entry_metrics(bars, len(bars) - 1) or {}
    ma5 = bot.volume_ma(bars, len(bars) - 1, 5)
    vol = float(cur["v"])
    return {
        "rsi14": cur.get("rsi"),
        "macd_dif": cur.get("macd_dif"),
        "macd_dea": cur.get("macd_dea"),
        "macd_hist": hist,
        "macd_hist_pct_close": (float(hist) / close * 100) if hist is not None and close else None,
        "adx14": adx,
        "obv_delta_5": obv5,
        "obv_rising_5": (obv5 > 0) if obv5 is not None else None,
        "stoch_k": long_metrics.get("stoch_k"),
        "stoch_d": long_metrics.get("stoch_d"),
        "volume": vol,
        "volume_ma5": ma5,
        "volume_vs_ma5_pct": (vol / ma5 - 1) * 100 if ma5 else None,
    }


def indicator_changes(before, after):
    """Numerical changes, not causal statements about why a trade won."""
    if not before or not after:
        return None
    names = (
        "rsi14", "macd_dif", "macd_dea", "macd_hist", "macd_hist_pct_close",
        "adx14", "obv_delta_5", "stoch_k", "stoch_d",
        "volume", "volume_vs_ma5_pct",
    )
    return {
        key: (float(after[key]) - float(before[key]))
        if before.get(key) is not None and after.get(key) is not None else None
        for key in names
    }


def live_executions_for_signal(state, signal_close_ms):
    """Read-only match against broker-tracked positions and closed trades."""
    executions = []
    seen = set()
    positions = state.get("positions") or {}
    if isinstance(positions, dict):
        position_values = positions.values()
    elif isinstance(positions, list):
        position_values = positions
    else:
        position_values = []
    for row in list(state.get("trade_history") or []) + list(position_values):
        if not isinstance(row, dict):
            continue
        if int(row.get("signal_close_ms") or 0) != signal_close_ms:
            continue
        inst = str(row.get("inst") or "")
        side = str(row.get("side") or "").upper()
        if not inst or side not in ("LONG", "SHORT"):
            continue
        key = (inst, side)
        if key in seen:
            continue
        seen.add(key)
        executions.append({
            "inst": inst, "side": side,
            "opened_ms": row.get("opened_ms"),
            "closed_ms": row.get("closed_ms"),
            "actual_net_pnl_usdt_if_closed": row.get("net_pnl_usdt"),
        })
    return executions


def refresh_real_live_trade_outcomes(history, state):
    """Replace paper assumptions with actual broker-recorded outcomes when available."""
    by_key = {
        (int(row.get("signal_close_ms") or 0), str(row.get("inst") or ""),
         str(row.get("side") or "").upper()): row
        for row in state.get("trade_history", [])
        if isinstance(row, dict)
    }
    changed = False
    for scan in history.get("scans", [])[-32:]:
        signal_ms = int(scan.get("signal_close_ms") or 0)
        for record in scan.get("live_executions_at_signal", []):
            trade = by_key.get((
                signal_ms,
                str(record.get("inst") or ""),
                str(record.get("side") or "").upper(),
            ))
            if trade is None:
                continue
            signal_row = next(
                (item for item in scan.get("instruments", [])
                 if item.get("inst") == record.get("inst")), {}
            )
            signal_price = float((signal_row.get("candle") or {}).get("close") or 0)
            actual_entry = float(trade.get("open_price") or 0)
            side = record.get("side")
            gap = (
                (actual_entry / signal_price - 1) * 100
                if side == "LONG" and signal_price and actual_entry else
                (1 - actual_entry / signal_price) * 100
                if side == "SHORT" and signal_price and actual_entry else None
            )
            outcome = {
                "result": trade.get("result"),
                "actual_net_pnl_usdt_after_fees": trade.get("net_pnl_usdt"),
                "gross_pnl_usdt": trade.get("gross_pnl_usdt"),
                "fee_usdt": trade.get("fee_usdt"),
                "actual_open_price": trade.get("open_price"),
                "actual_close_price": trade.get("close_price"),
                "signal_close_price": signal_price or None,
                "actual_adverse_entry_gap_pct": gap,
                "closed_ms": trade.get("closed_ms"),
                "reason": trade.get("reason"),
            }
            if record.get("actual_closed_trade") != outcome:
                record["actual_closed_trade"] = outcome
                record["closed_ms"] = trade.get("closed_ms")
                record["actual_net_pnl_usdt_if_closed"] = trade.get("net_pnl_usdt")
                changed = True
    return changed


def rank_all_choices_after_15m(scan):
    """Ex-post rankings, strictly separated from information available at entry."""
    choices = []
    executed = {(r["inst"], r["side"]) for r in scan.get("live_executions_at_signal", [])}
    for row in scan.get("instruments", []):
        outcome = row.get("next_candle_review") or {}
        if outcome.get("status") != "DONE":
            continue
        for side in ("LONG", "SHORT"):
            result = outcome.get(side) or {}
            if not result:
                continue
            original = row.get(side.lower()) or {}
            missing = original.get("missing") if isinstance(original, dict) else None
            indicators_at_signal = row.get("indicators_at_signal") or {}
            indicator_comparison_fields = (
                "rsi14", "adx14", "obv_rising_5", "macd_hist_pct_close",
                "stoch_k", "stoch_d", "volume_vs_ma5_pct",
            )
            quality = next(
                (entry for entry in scan.get("pretrade_quality_ranking", {}).get("scores", [])
                 if entry.get("inst") == row.get("inst") and entry.get("side") == side),
                {},
            )
            entry = {
                "inst": row.get("inst"),
                "side": side,
                "pretrade_quality_score": quality.get("score"),
                "pretrade_qualified": quality.get("live_candidate"),
                "rank_at_signal": row.get("rank"),
                "indicators_at_signal": {
                    key: indicators_at_signal.get(key)
                    for key in indicator_comparison_fields
                },
                "indicator_changes_after_15m": outcome.get("indicator_change_over_15m"),
                "net_at_15m_close_pct_estimated": result.get("estimated_next_close_net_pct_after_assumed_fees"),
                "tp_sl_1pct_first_touch": result.get("outcome_1_0"),
                "tp_sl_0_5pct_first_touch": result.get("outcome_0_5"),
                "live_executed": (row.get("inst"), side) in executed,
                "passed_candle_filters_at_signal": not missing if missing is not None else None,
                "missing_candle_filters_at_signal": missing,
            }
            choices.append(entry)
    choices.sort(key=lambda x: x["net_at_15m_close_pct_estimated"], reverse=True)
    for position, entry in enumerate(choices, 1):
        entry["rank_ex_post"] = position
    eligible = [x for x in choices if x["passed_candle_filters_at_signal"] is True]
    selected = [x for x in choices if x["live_executed"]]
    best = choices[0] if choices else None
    best_eligible = eligible[0] if eligible else None
    return {
        "status": "DONE" if len(choices) == 14 else "PARTIAL",
        "interpretation": (
            "Hindsight only: entry at 15m signal candle CLOSE; exit after exactly "
            "one next 15m candle CLOSE with assumed 0.12% round-trip fees. "
            "Not actual fills, not a usable advance selection rule."
        ),
        "all_long_and_short_options": choices,
        "best_any_side_ex_post": best,
        "best_long_ex_post": next((x for x in choices if x["side"] == "LONG"), None),
        "best_short_ex_post": next((x for x in choices if x["side"] == "SHORT"), None),
        "best_passing_initial_candle_filters_ex_post": best_eligible,
        "actual_live_selections_in_ranking": selected,
        "actual_live_choice_different_from_ex_post_best": (
            any((x["inst"], x["side"]) != (best["inst"], best["side"]) for x in selected)
            if selected and best else None
        ),
    }


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
            bars_through_next = [bar for bar in bars if bot.bar_close_ms(bar) <= target_close_ms]
            indicator_after = (
                snapshot_indicators(bars_through_next)
                if bars_through_next and bot.bar_close_ms(bars_through_next[-1]) == target_close_ms
                else None
            )
            row["next_candle_review"] = {
                "status": "DONE",
                "next_candle_close_ms": target_close_ms,
                "open": float(following["o"]),
                "high": float(following["h"]),
                "low": float(following["l"]),
                "close": float(following["c"]),
                "LONG": outcome_for_side("LONG", float(entry), following),
                "SHORT": outcome_for_side("SHORT", float(entry), following),
                "indicators_after_15m": indicator_after,
                "indicator_change_over_15m": indicator_changes(
                    row.get("indicators_at_signal"), indicator_after
                ),
            }
            changed = True
        scan["next_candle_review_complete"] = all(
            row.get("next_candle_review", {}).get("status") in ("DONE", "NO_BASE", "EXPIRED")
            for row in scan.get("instruments", [])
        )
        scan["reviewed_at_utc"] = utc_now()
        scan["best_coin_comparison_after_15m"] = rank_all_choices_after_15m(scan)
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
        indicators_at_signal = None
        indicator_error = None
        if inst and candle.get("close"):
            bars = fetch_cached(inst, cache)
            if isinstance(bars, Exception):
                indicator_error = str(bars)
            else:
                closed_bars = [bar for bar in bars if bot.bar_close_ms(bar) <= signal_close_ms]
                if closed_bars and bot.bar_close_ms(closed_bars[-1]) == signal_close_ms:
                    indicators_at_signal = snapshot_indicators(closed_bars)
                    adx14 = indicators_at_signal["adx14"]
                    obv_delta_5 = indicators_at_signal["obv_delta_5"]
                else:
                    indicator_error = "Signal candle not in available closed-bar history"
                    print(f"AUDIT_CANDLE_MISSING {inst} {signal_close_ms}")
        instruments.append({
            "inst": inst,
            "rank": item.get("rank"),
            "signal_close_ms": item.get("close_ms"),
            "candle": candle,
            "rsi14": item.get("rsi14"),
            "indicators_at_signal": indicators_at_signal,
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
        "live_executions_at_signal": live_executions_for_signal(state, signal_close_ms),
        "pretrade_quality_ranking": (
            state.get("last_pretrade_ranking")
            if int((state.get("last_pretrade_ranking") or {}).get("generated_at_ms") or 0)
            >= signal_close_ms else {"status": "UNAVAILABLE"}
        ),
        "top7": state.get("last_top7") or diagnostic.get("top7"),
        "instruments": instruments,
        "next_candle_review_complete": False,
        "best_coin_comparison_after_15m": {"status": "PENDING"},
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
    updated = refresh_real_live_trade_outcomes(history, bot.load_state()) or updated
    if updated or not HISTORY_FILE.exists():
        history["updated_at_utc"] = utc_now()
        history["scan_count"] = len(history["scans"])
        save_json(HISTORY_FILE, history)
        print(f"AUDIT_HISTORY_UPDATED path={HISTORY_FILE} scans={history['scan_count']}")
    else:
        print("AUDIT_HISTORY_UNCHANGED")


if __name__ == "__main__":
    main()
