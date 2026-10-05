#!/usr/bin/env python3
import csv
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from blofin_alert_15m import (
    top_usdt_swaps,
    analyse_candidate,
    observe_market,
    candles,
    MIN_TARGET_PCT,
    MIN_HIT_RATE,
    MIN_DECIDED,
)

SCAN_LOG_DIR = Path("blofin_live_scans")
SUMMARY_DIR = Path("blofin_live_scan_summary")
TRADE_LOG_DIR = Path("blofin_live_trades")

HORIZONS = (1, 2, 4, 8)
TP_PCT = 0.5
SL_PCT = 0.5

# Analysis-only costs. They never change order placement.
ANALYSIS_ROUND_TRIP_FEE_PCT = 0.12
ANALYSIS_ROUND_TRIP_SLIPPAGE_PCT = 0.0


def finite(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None


def aligned(direction, bullish):
    if direction == "LONG":
        return bool(bullish)
    if direction == "SHORT":
        return not bool(bullish)
    return None


def pass_fail(value):
    if value is None:
        return "N/A"
    return "PASS" if value else "FAIL"


def entry_conditions(analysis):
    if not isinstance(analysis, dict) or not analysis.get("direction"):
        return {
            "direction_signal": "FAIL",
            "stochastic_direction": "FAIL",
            "momentum_8_bars": "N/A",
            "target_distance": "N/A",
            "historical_sample_size": "N/A",
            "historical_hit_rate": "N/A",
            "overall_entry": "FAIL",
        }

    direction = analysis.get("direction")
    k = safe_float(analysis.get("stoch_k_8", analysis.get("k")))
    d = safe_float(analysis.get("stoch_d_3", analysis.get("d")))
    change8 = safe_float(analysis.get("change8_pct", analysis.get("change8")))
    target_pct = safe_float(analysis.get("target_pct"))
    decided = int(analysis.get("decided") or 0)
    hit_rate = safe_float(analysis.get("hit_rate"))

    stoch_ok = None
    if k is not None and d is not None:
        stoch_ok = (k > d and k > 50) if direction == "LONG" else (k < d and k < 50)

    momentum_ok = None
    if change8 is not None:
        momentum_ok = change8 > 0 if direction == "LONG" else change8 < 0

    target_ok = target_pct is not None and target_pct >= float(MIN_TARGET_PCT)
    sample_ok = decided >= int(MIN_DECIDED)
    hit_ok = hit_rate is not None and hit_rate >= float(MIN_HIT_RATE)

    return {
        "direction_signal": "PASS",
        "stochastic_direction": pass_fail(stoch_ok),
        "momentum_8_bars": pass_fail(momentum_ok),
        "target_distance": pass_fail(target_ok),
        "historical_sample_size": pass_fail(sample_ok),
        "historical_hit_rate": pass_fail(hit_ok),
        "overall_entry": pass_fail(bool(analysis.get("passed"))),
    }


def diagnostic_checks(direction, obs):
    if not isinstance(obs, dict) or direction not in ("LONG", "SHORT"):
        return {}

    out = {}

    volume = obs.get("volume") or {}
    out["volume_above_ma20"] = pass_fail(volume.get("vs_ma20") == "ABOVE" if volume.get("vs_ma20") else None)

    rsi = obs.get("rsi14") or {}
    rsi_val = safe_float(rsi.get("value"))
    out["rsi_directional"] = pass_fail((rsi_val >= 50) if direction == "LONG" else (rsi_val <= 50) if rsi_val is not None else None)

    macd = obs.get("macd_12_26_9") or {}
    pos = macd.get("position")
    out["macd_directional"] = pass_fail((pos == "BULLISH") if direction == "LONG" else (pos == "BEARISH") if pos else None)

    stoch = obs.get("stochastic_8_3") or {}
    spos = stoch.get("position")
    out["stochastic_k_vs_d"] = pass_fail((spos == "K_ABOVE_D") if direction == "LONG" else (spos == "K_BELOW_D") if spos else None)

    adx = obs.get("adx14") or {}
    adx_val = safe_float(adx.get("value"))
    out["adx_25_plus"] = pass_fail(adx_val >= 25 if adx_val is not None else None)

    obv = obs.get("obv") or {}
    otrend = obv.get("trend_5")
    out["obv_directional"] = pass_fail((otrend == "RISING") if direction == "LONG" else (otrend == "FALLING") if otrend else None)

    ema = obs.get("ema200") or {}
    epos = ema.get("price_position")
    out["ema200_directional"] = pass_fail((epos == "ABOVE") if direction == "LONG" else (epos == "BELOW") if epos else None)

    cvd = obs.get("cvd_proxy") or {}
    ctrend = cvd.get("trend_5")
    out["cvd_proxy_directional"] = pass_fail((ctrend == "RISING") if direction == "LONG" else (ctrend == "FALLING") if ctrend else None)

    candle = obs.get("candle") or {}
    color = candle.get("color")
    out["candle_directional"] = pass_fail((color == "GREEN") if direction == "LONG" else (color == "RED") if color else None)

    return out


def horizon_result(direction, entry, future, n):
    if len(future) < n:
        return None

    w = future[:n]
    final_close = float(w[-1]["c"])
    highs = [float(x["h"]) for x in w]
    lows = [float(x["l"]) for x in w]

    raw_close = (final_close / entry - 1.0) * 100.0
    max_up = (max(highs) / entry - 1.0) * 100.0
    max_down = (entry - min(lows)) / entry * 100.0

    result = {
        "bars": n,
        "end_bar_time": int(w[-1]["t"]),
        "close_change_pct": raw_close,
        "max_up_pct": max_up,
        "max_down_pct": max_down,
    }

    if direction not in ("LONG", "SHORT"):
        result.update({
            "directional_close_pct": None,
            "mfe_pct": None,
            "mae_pct": None,
            "tp_0_5_hit": None,
            "sl_0_5_hit": None,
            "first_event": "NO_DIRECTION",
            "estimated_net_close_pct_after_costs": None,
        })
        return result

    if direction == "LONG":
        directional_close = raw_close
        mfe = max_up
        mae = max_down
    else:
        directional_close = (entry / final_close - 1.0) * 100.0
        mfe = max_down
        mae = max_up

    first_event = "NONE"
    tp_hit = False
    sl_hit = False

    for bar in w:
        if direction == "LONG":
            this_tp = (float(bar["h"]) / entry - 1.0) * 100.0 >= TP_PCT
            this_sl = (entry - float(bar["l"])) / entry * 100.0 >= SL_PCT
        else:
            this_tp = (entry - float(bar["l"])) / entry * 100.0 >= TP_PCT
            this_sl = (float(bar["h"]) / entry - 1.0) * 100.0 >= SL_PCT

        tp_hit = tp_hit or this_tp
        sl_hit = sl_hit or this_sl

        if first_event == "NONE":
            if this_tp and this_sl:
                first_event = "BOTH_SAME_CANDLE"
            elif this_tp:
                first_event = "TP"
            elif this_sl:
                first_event = "SL"

    total_cost = ANALYSIS_ROUND_TRIP_FEE_PCT + ANALYSIS_ROUND_TRIP_SLIPPAGE_PCT
    result.update({
        "directional_close_pct": directional_close,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "tp_0_5_hit": tp_hit,
        "sl_0_5_hit": sl_hit,
        "first_event": first_event,
        "estimated_round_trip_fee_pct": ANALYSIS_ROUND_TRIP_FEE_PCT,
        "estimated_round_trip_slippage_pct": ANALYSIS_ROUND_TRIP_SLIPPAGE_PCT,
        "estimated_net_close_pct_after_costs": directional_close - total_cost,
    })
    return result


def scan_payload():
    top = top_usdt_swaps()
    analysed = {}
    observed = {}
    candidates = []

    for row in top:
        inst = row["inst"]
        try:
            observed[inst] = observe_market(inst)
        except Exception as exc:
            observed[inst] = {"error": str(exc)}

        try:
            a = analyse_candidate(inst)
            analysed[inst] = a
            if a:
                a["change24"] = row.get("change24")
                if a.get("passed"):
                    candidates.append(a)
        except Exception as exc:
            analysed[inst] = {"passed": False, "reasons": [f"analysis error: {exc}"]}

    candidates.sort(
        key=lambda x: (
            float(x.get("hit_rate") or 0),
            float(x.get("target_pct") or 0),
            float(x.get("change24") or 0),
        ),
        reverse=True,
    )
    selected = candidates[0] if candidates else None

    now_utc = datetime.now(timezone.utc)
    now_uk = now_utc.astimezone(ZoneInfo("Europe/London"))

    rows = []
    for market in top:
        inst = market["inst"]
        a = analysed.get(inst)
        obs = observed.get(inst)
        direction = a.get("direction") if isinstance(a, dict) else None
        decision = "WEJSCIE" if isinstance(a, dict) and a.get("passed") else "NIE_WCHODZIC" if a else "BRAK_SYGNALU_KIERUNKOWEGO"

        rows.append({
            "instrument": inst,
            "market_last_price": market.get("last"),
            "change24_pct": market.get("change24"),
            "decision": decision,
            "entry_conditions": entry_conditions(a),
            "diagnostic_checks_not_used_for_entry": diagnostic_checks(direction, obs),
            "analysis": a,
            "indicators_observed": obs,
            "multi_horizon_review": {},
        })

    return {
        "scan_time_utc": now_utc.isoformat(),
        "scan_time_uk": now_uk.isoformat(),
        "scanner_thresholds": {
            "target_pct_min": MIN_TARGET_PCT,
            "historical_hit_rate_pct_min": MIN_HIT_RATE,
            "minimum_decided_samples": MIN_DECIDED,
            "live_tp_pct": TP_PCT,
            "live_sl_pct": SL_PCT,
        },
        "analysis_cost_assumptions": {
            "round_trip_fee_pct": ANALYSIS_ROUND_TRIP_FEE_PCT,
            "round_trip_slippage_pct": ANALYSIS_ROUND_TRIP_SLIPPAGE_PCT,
            "note": "analysis only; does not change live order placement",
        },
        "scanned": rows,
        "selected_for_entry": selected,
        "multi_horizon_review_complete": False,
        "analytics_version": 2,
    }


def ensure_current_scan():
    SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    latest = None
    for path in sorted(SCAN_LOG_DIR.glob("*.json"), reverse=True)[:4]:
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
            t = datetime.fromisoformat(str(p.get("scan_time_utc")).replace("Z", "+00:00"))
            if latest is None or t > latest:
                latest = t
        except Exception:
            pass

    # The live trader normally writes the scan first. Create an analytics-only
    # scan only when no fresh scan exists, e.g. while a position is already open.
    if latest and (now - latest) <= timedelta(minutes=7):
        return None

    payload = scan_payload()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = SCAN_LOG_DIR / f"{stamp}_analytics.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"ANALYTICS_SCAN {path}")
    return path


def normalize_existing_scan(payload):
    for row in payload.get("scanned", []):
        a = row.get("analysis") if isinstance(row.get("analysis"), dict) else None
        obs = row.get("indicators_observed") if isinstance(row.get("indicators_observed"), dict) else {}
        direction = a.get("direction") if a else None
        row["entry_conditions"] = entry_conditions(a)
        row["diagnostic_checks_not_used_for_entry"] = diagnostic_checks(direction, obs)
        row.setdefault("multi_horizon_review", {})

    payload["analysis_cost_assumptions"] = {
        "round_trip_fee_pct": ANALYSIS_ROUND_TRIP_FEE_PCT,
        "round_trip_slippage_pct": ANALYSIS_ROUND_TRIP_SLIPPAGE_PCT,
        "note": "analysis only; does not change live order placement",
    }
    payload["analytics_version"] = 2
    return payload


def update_reviews():
    SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SCAN_LOG_DIR.glob("*.json"), reverse=True)[:96]
    loaded = []

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        normalize_existing_scan(payload)
        loaded.append((path, payload))

    instruments = set()
    for _, payload in loaded:
        if payload.get("multi_horizon_review_complete"):
            continue
        for row in payload.get("scanned", []):
            instruments.add(row.get("instrument"))

    candle_cache = {}
    for inst in sorted(x for x in instruments if x):
        try:
            candle_cache[inst] = candles(inst)
        except Exception as exc:
            candle_cache[inst] = {"error": str(exc)}

    touched_dates = set()

    for path, payload in loaded:
        changed = False
        all_complete = True

        for row in payload.get("scanned", []):
            a = row.get("analysis") if isinstance(row.get("analysis"), dict) else {}
            obs = row.get("indicators_observed") if isinstance(row.get("indicators_observed"), dict) else {}
            direction = a.get("direction")
            base_time = a.get("bar_time") or obs.get("bar_time")
            entry = safe_float(a.get("close") or obs.get("price") or row.get("market_last_price"))
            inst = row.get("instrument")

            if not base_time or not entry or not inst:
                row["review_status"] = "NO_BASE_DATA"
                continue

            bars = candle_cache.get(inst)
            if not isinstance(bars, list):
                row["review_status"] = "CANDLE_FETCH_ERROR"
                all_complete = False
                continue

            future = [b for b in bars if int(b.get("t") or 0) > int(base_time)]
            reviews = row.setdefault("multi_horizon_review", {})

            for h in HORIZONS:
                key = f"{h}_bars"
                if key not in reviews and len(future) >= h:
                    reviews[key] = horizon_result(direction, entry, future, h)
                    changed = True

            one = reviews.get("1_bars")
            if one is not None:
                row["next_candle_direction_confirmed"] = (
                    one.get("directional_close_pct") is not None and one.get("directional_close_pct") > 0
                )
                row["next_candle_hit_tp_0_5_pct"] = one.get("tp_0_5_hit")

            complete = all(f"{h}_bars" in reviews for h in HORIZONS)
            row["review_status"] = "COMPLETE" if complete else "WAITING"
            if not complete:
                all_complete = False

        if payload.get("multi_horizon_review_complete") != all_complete:
            payload["multi_horizon_review_complete"] = all_complete
            changed = True

        payload["analytics_reviewed_at_utc"] = datetime.now(timezone.utc).isoformat()

        if changed:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        try:
            uk_date = datetime.fromisoformat(str(payload.get("scan_time_uk")).replace("Z", "+00:00")).date().isoformat()
            touched_dates.add(uk_date)
        except Exception:
            pass

    return touched_dates


def flatten_obs(row):
    obs = row.get("indicators_observed") or {}
    analysis = row.get("analysis") or {}
    rsi = obs.get("rsi14") or {}
    macd = obs.get("macd_12_26_9") or {}
    stoch = obs.get("stochastic_8_3") or {}
    adx = obs.get("adx14") or {}
    vol = obs.get("volume") or {}
    obv = obs.get("obv") or {}
    ema = obs.get("ema200") or {}
    cvd = obs.get("cvd_proxy") or {}

    return {
        "direction": analysis.get("direction"),
        "target_pct": analysis.get("target_pct"),
        "hit_rate_pct": analysis.get("hit_rate"),
        "decided_samples": analysis.get("decided"),
        "change8_pct": analysis.get("change8_pct"),
        "change24_pct": row.get("change24_pct"),
        "rsi14": rsi.get("value"),
        "rsi_vs_ma9": rsi.get("vs_ma9"),
        "macd_hist": macd.get("histogram"),
        "macd_position": macd.get("position"),
        "stoch_k": stoch.get("k"),
        "stoch_d": stoch.get("d"),
        "adx14": adx.get("value"),
        "volume_vs_ma20": vol.get("vs_ma20"),
        "obv_trend5": obv.get("trend_5"),
        "ema200_position": ema.get("price_position"),
        "cvd_proxy_trend5": cvd.get("trend_5"),
    }


def load_trade_metrics():
    metrics = {}
    if not TRADE_LOG_DIR.exists():
        return metrics

    for path in TRADE_LOG_DIR.glob("*.json"):
        try:
            t = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        sig = t.get("signal_snapshot") or {}
        key = (t.get("instrument"), sig.get("bar_time"), t.get("direction"))
        notional = safe_float(t.get("notional_usdt"))
        fee = safe_float(t.get("fee_usdt"))
        fee_pct = (fee / notional * 100.0) if fee is not None and notional else None

        hist = t.get("blofin_position_history") or {}
        fill = None
        for k in ("averagePrice", "avgPrice", "openAvgPrice", "openPrice", "entryPrice"):
            v = safe_float(hist.get(k))
            if v and v > 0:
                fill = v
                break

        ref = safe_float(t.get("reference_entry_price"))
        direction = t.get("direction")
        slip = None
        if fill and ref:
            if direction == "LONG":
                slip = (fill / ref - 1.0) * 100.0
            elif direction == "SHORT":
                slip = (ref / fill - 1.0) * 100.0

        metrics[key] = {
            "actual_trade": True,
            "actual_fee_usdt": fee,
            "actual_fee_pct_of_notional": fee_pct,
            "actual_entry_slippage_pct_if_available": slip,
            "actual_net_pnl_usdt": safe_float(t.get("net_pnl_usdt")),
        }

    return metrics


def regenerate_daily_csv(date_str):
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    trade_metrics = load_trade_metrics()
    rows = []

    for path in sorted(SCAN_LOG_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            dt = datetime.fromisoformat(str(payload.get("scan_time_uk")).replace("Z", "+00:00"))
        except Exception:
            continue
        if dt.date().isoformat() != date_str:
            continue

        for row in payload.get("scanned", []):
            a = row.get("analysis") or {}
            base = {
                "scan_time_uk": payload.get("scan_time_uk"),
                "instrument": row.get("instrument"),
                "decision": row.get("decision"),
                "selected_for_entry": bool(
                    payload.get("selected_for_entry")
                    and payload.get("selected_for_entry", {}).get("inst") == row.get("instrument")
                    and payload.get("selected_for_entry", {}).get("bar_time") == a.get("bar_time")
                ),
                **flatten_obs(row),
                "entry_conditions": json.dumps(row.get("entry_conditions") or {}, ensure_ascii=False, separators=(",", ":")),
                "diagnostic_checks": json.dumps(row.get("diagnostic_checks_not_used_for_entry") or {}, ensure_ascii=False, separators=(",", ":")),
                "analysis_round_trip_fee_pct": ANALYSIS_ROUND_TRIP_FEE_PCT,
                "analysis_round_trip_slippage_pct": ANALYSIS_ROUND_TRIP_SLIPPAGE_PCT,
                "next_candle_direction_confirmed": row.get("next_candle_direction_confirmed"),
            }

            reviews = row.get("multi_horizon_review") or {}
            for h in HORIZONS:
                r = reviews.get(f"{h}_bars") or {}
                prefix = f"h{h}"
                base[f"{prefix}_directional_close_pct"] = r.get("directional_close_pct")
                base[f"{prefix}_mfe_pct"] = r.get("mfe_pct")
                base[f"{prefix}_mae_pct"] = r.get("mae_pct")
                base[f"{prefix}_tp_hit"] = r.get("tp_0_5_hit")
                base[f"{prefix}_sl_hit"] = r.get("sl_0_5_hit")
                base[f"{prefix}_first_event"] = r.get("first_event")
                base[f"{prefix}_net_close_after_costs_pct"] = r.get("estimated_net_close_pct_after_costs")

            key = (row.get("instrument"), a.get("bar_time"), a.get("direction"))
            tm = trade_metrics.get(key, {})
            base["actual_trade"] = tm.get("actual_trade", False)
            base["actual_fee_usdt"] = tm.get("actual_fee_usdt")
            base["actual_fee_pct_of_notional"] = tm.get("actual_fee_pct_of_notional")
            base["actual_entry_slippage_pct_if_available"] = tm.get("actual_entry_slippage_pct_if_available")
            base["actual_net_pnl_usdt"] = tm.get("actual_net_pnl_usdt")
            rows.append(base)

    if not rows:
        return None

    fields = list(rows[0].keys())
    path = SUMMARY_DIR / f"{date_str}.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"DAILY_CSV {path} rows={len(rows)}")
    return path


def main():
    ensure_current_scan()
    dates = update_reviews()

    now_uk = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    dates.add(now_uk)

    for date_str in sorted(dates):
        regenerate_daily_csv(date_str)


if __name__ == "__main__":
    main()
