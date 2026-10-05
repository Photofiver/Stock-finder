#!/usr/bin/env python3
import csv
import json
import math
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from blofin_alert_15m import (
    top_usdt_swaps,
    analyse_candidate,
    observe_market,
    candles,
    get_json,
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

# Metadata only. These values do not change live entry logic.
STRATEGY_VERSION = "live-entry-v1"
ANALYTICS_VERSION = 4
GIT_COMMIT_SHA = os.environ.get("GITHUB_SHA", "").strip() or None
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "").strip() or None
GITHUB_RUN_ATTEMPT = os.environ.get("GITHUB_RUN_ATTEMPT", "").strip() or None


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


def make_scan_id(scan_time_utc):
    seed = str(scan_time_utc or datetime.now(timezone.utc).isoformat())
    return uuid.uuid5(uuid.NAMESPACE_URL, "Photofiver/Stock-finder:" + seed).hex


def scan_time_features(dt_uk):
    return {
        "hour_uk": dt_uk.hour,
        "minute_uk": dt_uk.minute,
        "weekday_number": dt_uk.weekday(),
        "weekday_name": dt_uk.strftime("%A"),
        "is_weekend": dt_uk.weekday() >= 5,
    }


def decision_latency_ms(scan_time_utc, analysis, obs):
    base_time = None
    if isinstance(analysis, dict):
        base_time = analysis.get("bar_time")
    if not base_time and isinstance(obs, dict):
        base_time = obs.get("bar_time")
    try:
        base_time = int(base_time)
    except Exception:
        return None

    # bar_time is the OPEN of the last fully closed 15m candle.
    candle_close_ms = base_time + 15 * 60 * 1000
    scan_ms = int(scan_time_utc.timestamp() * 1000)
    return max(0, scan_ms - candle_close_ms)


def rejection_details(analysis):
    if not isinstance(analysis, dict) or not analysis.get("direction"):
        reasons = [str(x) for x in ((analysis or {}).get("reasons") or [])] if isinstance(analysis, dict) else []
        codes = ["NO_DIRECTION_SIGNAL"]
        if any(x.startswith("analysis error:") for x in reasons):
            codes.append("ANALYSIS_ERROR")
        return {
            "rejected": True,
            "reason_codes": codes,
            "reason_text": reasons or ["brak pełnego sygnału kierunkowego"],
        }

    if analysis.get("passed"):
        return {"rejected": False, "reason_codes": [], "reason_text": []}

    reasons = [str(x) for x in (analysis.get("reasons") or [])]
    codes = []

    target_pct = safe_float(analysis.get("target_pct"))
    hit_rate = safe_float(analysis.get("hit_rate"))
    decided = int(analysis.get("decided") or 0)

    if target_pct is None:
        codes.append("NO_SUPPORT_RESISTANCE_TARGET")
    elif target_pct < float(MIN_TARGET_PCT):
        codes.append("TARGET_TOO_CLOSE")

    if decided < int(MIN_DECIDED):
        codes.append("NOT_ENOUGH_HISTORY")

    if hit_rate is None or hit_rate < float(MIN_HIT_RATE):
        codes.append("HIT_RATE_TOO_LOW")

    for reason in reasons:
        if reason.startswith("analysis error:"):
            codes.append("ANALYSIS_ERROR")

    if not codes:
        codes.append("OTHER_FILTER_FAIL")

    return {
        "rejected": True,
        "reason_codes": sorted(set(codes)),
        "reason_text": reasons,
    }


def btc_market_context():
    try:
        obs = observe_market("BTC-USDT")
        if not isinstance(obs, dict) or obs.get("error"):
            return {"instrument": "BTC-USDT", "error": (obs or {}).get("error") if isinstance(obs, dict) else "invalid response"}

        ema = obs.get("ema200") or {}
        macd = obs.get("macd_12_26_9") or {}
        adx = obs.get("adx14") or {}
        atr = obs.get("atr14") or {}
        rsi = obs.get("rsi14") or {}
        change8 = safe_float(obs.get("change8_pct"))

        above_ema = ema.get("price_position") == "ABOVE"
        below_ema = ema.get("price_position") == "BELOW"
        macd_bull = macd.get("position") == "BULLISH"
        macd_bear = macd.get("position") == "BEARISH"

        if above_ema and macd_bull and change8 is not None and change8 > 0:
            trend = "BULLISH"
        elif below_ema and macd_bear and change8 is not None and change8 < 0:
            trend = "BEARISH"
        else:
            trend = "MIXED"

        return {
            "instrument": "BTC-USDT",
            "trend": trend,
            "price": obs.get("price"),
            "change8_pct": change8,
            "atr14": atr.get("value"),
            "atr14_pct_of_price": atr.get("pct_of_price"),
            "adx14": adx.get("value"),
            "rsi14": rsi.get("value"),
            "ema200_position": ema.get("price_position"),
            "macd_position": macd.get("position"),
            "macd_histogram": macd.get("histogram"),
        }
    except Exception as exc:
        return {"instrument": "BTC-USDT", "error": str(exc)}


def market_context_for(instruments):
    out = {inst: {"market_microstructure": {}, "funding": {}, "errors": []} for inst in instruments}

    try:
        tickers = get_json("/api/v1/market/tickers").get("data", [])
        ticker_map = {x.get("instId"): x for x in tickers if x.get("instId")}
    except Exception as exc:
        ticker_map = {}
        for inst in instruments:
            out[inst]["errors"].append(f"ticker_fetch_error: {exc}")

    for inst in instruments:
        t = ticker_map.get(inst) or {}
        bid = safe_float(t.get("bidPrice"))
        ask = safe_float(t.get("askPrice"))
        last = safe_float(t.get("last"))
        spread_abs = (ask - bid) if bid is not None and ask is not None else None
        mid = ((ask + bid) / 2.0) if bid is not None and ask is not None else None
        spread_pct_mid = (spread_abs / mid * 100.0) if spread_abs is not None and mid else None

        out[inst]["market_microstructure"] = {
            "bid_price": bid,
            "ask_price": ask,
            "bid_size": safe_float(t.get("bidSize")),
            "ask_size": safe_float(t.get("askSize")),
            "last_price": last,
            "ticker_ts_ms": int(t.get("ts")) if str(t.get("ts") or "").isdigit() else None,
            "spread_abs": spread_abs,
            "spread_pct_mid": spread_pct_mid,
            "volume24h": safe_float(t.get("vol24h")),
            "volume_currency24h": safe_float(t.get("volCurrency24h")),
            "volume_usd24h": safe_float(t.get("volUsd24h")),
            "open24h": safe_float(t.get("open24h")),
            "high24h": safe_float(t.get("high24h")),
            "low24h": safe_float(t.get("low24h")),
        }

        if bid is None or ask is None:
            out[inst]["errors"].append("missing_bid_or_ask")

        try:
            fr = get_json("/api/v1/market/funding-rate", {"instId": inst}).get("data", [])
            fr = fr[0] if fr else {}
            rate = safe_float(fr.get("fundingRate"))
            out[inst]["funding"] = {
                "funding_rate": rate,
                "funding_rate_pct": rate * 100.0 if rate is not None else None,
                "funding_time_ms": int(fr.get("fundingTime")) if str(fr.get("fundingTime") or "").isdigit() else None,
                "funding_interval": fr.get("fundingInterval"),
                "funding_interval_unit": fr.get("fundingIntervalUnit"),
                "funding_rate_cap": safe_float(fr.get("fundingRateCap")),
                "funding_rate_floor": safe_float(fr.get("fundingRateFloor")),
            }
            if rate is None:
                out[inst]["errors"].append("missing_funding_rate")
        except Exception as exc:
            out[inst]["funding"] = {}
            out[inst]["errors"].append(f"funding_fetch_error: {exc}")

    return out


def data_quality_flags(row):
    flags = []
    obs = row.get("indicators_observed")
    analysis = row.get("analysis")
    micro = row.get("market_microstructure") or {}
    funding = row.get("funding") or {}

    if not isinstance(obs, dict):
        flags.append("missing_indicators_observed")
    elif obs.get("error"):
        flags.append("indicator_observation_error")
    else:
        required = (
            "volume", "rsi14", "macd_12_26_9", "stochastic_8_3", "adx14",
            "obv", "ema200", "bollinger_20_2", "donchian20", "atr14",
            "pivot_daily", "cvd_proxy", "support_resistance",
        )
        for key in required:
            if key not in obs or obs.get(key) is None:
                flags.append(f"missing_indicator:{key}")

    if isinstance(analysis, dict):
        for reason in analysis.get("reasons") or []:
            if str(reason).startswith("analysis error:"):
                flags.append("analysis_error")

    if micro.get("bid_price") is None:
        flags.append("missing_bid_price")
    if micro.get("ask_price") is None:
        flags.append("missing_ask_price")
    if funding.get("funding_rate") is None:
        flags.append("missing_funding_rate")

    for err in row.get("market_context_errors") or []:
        flags.append(str(err))

    return {
        "ok": len(flags) == 0,
        "flag_count": len(flags),
        "flags": sorted(set(flags)),
    }


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
    out["rsi_directional"] = pass_fail(None if rsi_val is None else ((rsi_val >= 50) if direction == "LONG" else (rsi_val <= 50)))

    macd = obs.get("macd_12_26_9") or {}
    pos = macd.get("position")
    out["macd_directional"] = pass_fail(None if not pos else ((pos == "BULLISH") if direction == "LONG" else (pos == "BEARISH")))

    stoch = obs.get("stochastic_8_3") or {}
    spos = stoch.get("position")
    out["stochastic_k_vs_d"] = pass_fail(None if not spos else ((spos == "K_ABOVE_D") if direction == "LONG" else (spos == "K_BELOW_D")))

    adx = obs.get("adx14") or {}
    adx_val = safe_float(adx.get("value"))
    out["adx_25_plus"] = pass_fail(adx_val >= 25 if adx_val is not None else None)

    obv = obs.get("obv") or {}
    otrend = obv.get("trend_5")
    out["obv_directional"] = pass_fail(None if not otrend else ((otrend == "RISING") if direction == "LONG" else (otrend == "FALLING")))

    ema = obs.get("ema200") or {}
    epos = ema.get("price_position")
    out["ema200_directional"] = pass_fail(None if not epos else ((epos == "ABOVE") if direction == "LONG" else (epos == "BELOW")))

    cvd = obs.get("cvd_proxy") or {}
    ctrend = cvd.get("trend_5")
    out["cvd_proxy_directional"] = pass_fail(None if not ctrend else ((ctrend == "RISING") if direction == "LONG" else (ctrend == "FALLING")))

    candle = obs.get("candle") or {}
    color = candle.get("color")
    out["candle_directional"] = pass_fail(None if not color else ((color == "GREEN") if direction == "LONG" else (color == "RED")))

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
    market_context = market_context_for([x["inst"] for x in top])
    btc_context = btc_market_context()
    for market in top:
        inst = market["inst"]
        a = analysed.get(inst)
        obs = observed.get(inst)
        direction = a.get("direction") if isinstance(a, dict) else None
        decision = "WEJSCIE" if isinstance(a, dict) and a.get("passed") else "NIE_WCHODZIC" if a else "BRAK_SYGNALU_KIERUNKOWEGO"

        latency = decision_latency_ms(now_utc, a, obs)
        reject = rejection_details(a)

        rows.append({
            "instrument": inst,
            "market_last_price": market.get("last"),
            "change24_pct": market.get("change24"),
            "decision": decision,
            "rejection": reject,
            "decision_latency_ms": latency,
            "decision_latency_seconds": (latency / 1000.0) if latency is not None else None,
            "entry_conditions": entry_conditions(a),
            "diagnostic_checks_not_used_for_entry": diagnostic_checks(direction, obs),
            "analysis": a,
            "indicators_observed": obs,
            "market_microstructure": market_context.get(inst, {}).get("market_microstructure", {}),
            "funding": market_context.get(inst, {}).get("funding", {}),
            "market_context_errors": market_context.get(inst, {}).get("errors", []),
            "multi_horizon_review": {},
        })

    payload = {
        "scan_id": make_scan_id(now_utc.isoformat()),
        "scan_time_utc": now_utc.isoformat(),
        "scan_time_uk": now_uk.isoformat(),
        "strategy_version": STRATEGY_VERSION,
        "git_commit_sha": GIT_COMMIT_SHA,
        "github_run_id": GITHUB_RUN_ID,
        "github_run_attempt": GITHUB_RUN_ATTEMPT,
        "scan_time_features": scan_time_features(now_uk),
        "btc_market_context": btc_context,
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
        "analytics_version": ANALYTICS_VERSION,
    }

    for row in payload["scanned"]:
        row["data_quality"] = data_quality_flags(row)

    return payload


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

    # The live trader normally writes the scan first. If it is fresh, enrich
    # that exact scan with market context/metadata instead of creating a duplicate.
    if latest and (now - latest) <= timedelta(minutes=7):
        latest_path = None
        latest_time = None
        for path in sorted(SCAN_LOG_DIR.glob("*.json"), reverse=True)[:8]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                t = datetime.fromisoformat(str(payload.get("scan_time_utc")).replace("Z", "+00:00"))
            except Exception:
                continue
            if latest_time is None or t > latest_time:
                latest_time = t
                latest_path = path

        if latest_path is not None:
            try:
                payload = json.loads(latest_path.read_text(encoding="utf-8"))
                instruments = [r.get("instrument") for r in payload.get("scanned", []) if r.get("instrument")]
                context = market_context_for(instruments)
                btc_context = btc_market_context()
                payload.setdefault("scan_id", make_scan_id(payload.get("scan_time_utc")))
                payload["strategy_version"] = STRATEGY_VERSION
                payload["git_commit_sha"] = GIT_COMMIT_SHA
                payload["github_run_id"] = GITHUB_RUN_ID
                payload["github_run_attempt"] = GITHUB_RUN_ATTEMPT
                try:
                    scan_dt_utc = datetime.fromisoformat(str(payload.get("scan_time_utc")).replace("Z", "+00:00"))
                except Exception:
                    scan_dt_utc = now
                scan_dt_uk = scan_dt_utc.astimezone(ZoneInfo("Europe/London"))
                payload["scan_time_features"] = scan_time_features(scan_dt_uk)
                payload["btc_market_context"] = btc_context
                payload["market_context_captured_at_utc"] = now.isoformat()
                payload["market_context_note"] = "captured immediately after live scan; analytics only"
                for row in payload.get("scanned", []):
                    inst = row.get("instrument")
                    ctx = context.get(inst, {})
                    row["market_microstructure"] = ctx.get("market_microstructure", {})
                    row["funding"] = ctx.get("funding", {})
                    row["market_context_errors"] = ctx.get("errors", [])
                    a = row.get("analysis") if isinstance(row.get("analysis"), dict) else None
                    obs = row.get("indicators_observed") if isinstance(row.get("indicators_observed"), dict) else {}
                    latency = decision_latency_ms(scan_dt_utc, a, obs)
                    row["decision_latency_ms"] = latency
                    row["decision_latency_seconds"] = (latency / 1000.0) if latency is not None else None
                    row["rejection"] = rejection_details(a)
                    row["data_quality"] = data_quality_flags(row)
                payload["analytics_version"] = ANALYTICS_VERSION
                latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"ENRICHED_SCAN {latest_path}")
            except Exception as exc:
                print(f"ENRICH_SCAN_ERROR {type(exc).__name__}: {exc}")
        return None

    payload = scan_payload()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = SCAN_LOG_DIR / f"{stamp}_analytics.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"ANALYTICS_SCAN {path}")
    return path


def normalize_existing_scan(payload):
    payload.setdefault("scan_id", make_scan_id(payload.get("scan_time_utc")))
    payload.setdefault("strategy_version", STRATEGY_VERSION)
    payload.setdefault("git_commit_sha", GIT_COMMIT_SHA)
    payload.setdefault("github_run_id", GITHUB_RUN_ID)
    payload.setdefault("github_run_attempt", GITHUB_RUN_ATTEMPT)
    try:
        scan_dt_utc = datetime.fromisoformat(str(payload.get("scan_time_utc")).replace("Z", "+00:00"))
    except Exception:
        scan_dt_utc = datetime.now(timezone.utc)
    scan_dt_uk = scan_dt_utc.astimezone(ZoneInfo("Europe/London"))
    payload.setdefault("scan_time_features", scan_time_features(scan_dt_uk))
    payload.setdefault("btc_market_context", {})

    for row in payload.get("scanned", []):
        a = row.get("analysis") if isinstance(row.get("analysis"), dict) else None
        obs = row.get("indicators_observed") if isinstance(row.get("indicators_observed"), dict) else {}
        direction = a.get("direction") if a else None
        row["entry_conditions"] = entry_conditions(a)
        row["diagnostic_checks_not_used_for_entry"] = diagnostic_checks(direction, obs)
        row["rejection"] = rejection_details(a)
        latency = decision_latency_ms(scan_dt_utc, a, obs)
        row["decision_latency_ms"] = latency
        row["decision_latency_seconds"] = (latency / 1000.0) if latency is not None else None
        row.setdefault("market_microstructure", {})
        row.setdefault("funding", {})
        row.setdefault("market_context_errors", [])
        row["data_quality"] = data_quality_flags(row)
        row.setdefault("multi_horizon_review", {})

    payload["analysis_cost_assumptions"] = {
        "round_trip_fee_pct": ANALYSIS_ROUND_TRIP_FEE_PCT,
        "round_trip_slippage_pct": ANALYSIS_ROUND_TRIP_SLIPPAGE_PCT,
        "note": "analysis only; does not change live order placement",
    }
    payload["analytics_version"] = ANALYTICS_VERSION
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
    atr = obs.get("atr14") or {}

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
        "atr14": atr.get("value"),
        "atr14_pct_of_price": atr.get("pct_of_price"),
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
                "scan_id": payload.get("scan_id"),
                "scan_time_uk": payload.get("scan_time_uk"),
                "strategy_version": payload.get("strategy_version"),
                "git_commit_sha": payload.get("git_commit_sha"),
                "github_run_id": payload.get("github_run_id"),
                "hour_uk": (payload.get("scan_time_features") or {}).get("hour_uk"),
                "weekday_number": (payload.get("scan_time_features") or {}).get("weekday_number"),
                "weekday_name": (payload.get("scan_time_features") or {}).get("weekday_name"),
                "is_weekend": (payload.get("scan_time_features") or {}).get("is_weekend"),
                "btc_trend": (payload.get("btc_market_context") or {}).get("trend"),
                "btc_change8_pct": (payload.get("btc_market_context") or {}).get("change8_pct"),
                "btc_atr14_pct": (payload.get("btc_market_context") or {}).get("atr14_pct_of_price"),
                "btc_adx14": (payload.get("btc_market_context") or {}).get("adx14"),
                "btc_rsi14": (payload.get("btc_market_context") or {}).get("rsi14"),
                "instrument": row.get("instrument"),
                "decision": row.get("decision"),
                "rejected": (row.get("rejection") or {}).get("rejected"),
                "rejection_reason_codes": json.dumps((row.get("rejection") or {}).get("reason_codes") or [], ensure_ascii=False, separators=(",", ":")),
                "rejection_reason_text": json.dumps((row.get("rejection") or {}).get("reason_text") or [], ensure_ascii=False, separators=(",", ":")),
                "decision_latency_ms": row.get("decision_latency_ms"),
                "decision_latency_seconds": row.get("decision_latency_seconds"),
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
                "bid_price": (row.get("market_microstructure") or {}).get("bid_price"),
                "ask_price": (row.get("market_microstructure") or {}).get("ask_price"),
                "spread_abs": (row.get("market_microstructure") or {}).get("spread_abs"),
                "spread_pct_mid": (row.get("market_microstructure") or {}).get("spread_pct_mid"),
                "volume24h": (row.get("market_microstructure") or {}).get("volume24h"),
                "volume_currency24h": (row.get("market_microstructure") or {}).get("volume_currency24h"),
                "volume_usd24h": (row.get("market_microstructure") or {}).get("volume_usd24h"),
                "high24h": (row.get("market_microstructure") or {}).get("high24h"),
                "low24h": (row.get("market_microstructure") or {}).get("low24h"),
                "funding_rate": (row.get("funding") or {}).get("funding_rate"),
                "funding_rate_pct": (row.get("funding") or {}).get("funding_rate_pct"),
                "funding_time_ms": (row.get("funding") or {}).get("funding_time_ms"),
                "data_quality_ok": (row.get("data_quality") or {}).get("ok"),
                "data_quality_flag_count": (row.get("data_quality") or {}).get("flag_count"),
                "data_quality_flags": json.dumps((row.get("data_quality") or {}).get("flags") or [], ensure_ascii=False, separators=(",", ":")),
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
