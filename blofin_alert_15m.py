#!/usr/bin/env python3
import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

BASE = "https://openapi.blofin.com"
TOP_N = 10
BAR = "15m"
CANDLE_LIMIT = 1440
MIN_TARGET_PCT = 1.0
MIN_HIT_RATE = 65.0
MIN_DECIDED = 30
HORIZON_BARS = 8
SL_PCT = 1.0
DECISION_LOG_DIR = Path("blofin_decision_journal")

def get_json(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Stock-finder-15m-scan/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))

def top_usdt_swaps():
    instruments = get_json("/api/v1/market/instruments").get("data", [])
    tickers = get_json("/api/v1/market/tickers").get("data", [])
    live = {
        x.get("instId")
        for x in instruments
        if x.get("state") == "live"
        and x.get("instType") == "SWAP"
        and x.get("contractType") == "linear"
        and x.get("settleCurrency") == "USDT"
    }
    rows = []
    for x in tickers:
        inst = x.get("instId")
        if inst not in live:
            continue
        try:
            last = float(x.get("last", 0))
            open24 = float(x.get("open24h", 0))
        except (TypeError, ValueError):
            continue
        if last <= 0 or open24 <= 0:
            continue
        rows.append({
            "inst": inst,
            "last": last,
            "change24": (last / open24 - 1.0) * 100.0,
        })
    rows.sort(key=lambda z: (z["change24"], z["inst"]), reverse=True)
    return rows[:TOP_N]

def candles(inst):
    raw = get_json(
        "/api/v1/market/candles",
        {"instId": inst, "bar": BAR, "limit": CANDLE_LIMIT},
    ).get("data", [])
    bars = []
    for a in reversed(raw):
        try:
            bars.append({
                "t": int(a[0]),
                "o": float(a[1]),
                "h": float(a[2]),
                "l": float(a[3]),
                "c": float(a[4]),
                "v": float(a[5]),
            })
        except (ValueError, TypeError, IndexError):
            pass

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    interval_ms = 15 * 60 * 1000
    current_open = (now_ms // interval_ms) * interval_ms
    return [b for b in bars if b["t"] < current_open]

def atr(bars, n=14):
    out = [math.nan] * len(bars)
    tr = [0.0] * len(bars)
    for i, b in enumerate(bars):
        if i == 0:
            tr[i] = b["h"] - b["l"]
        else:
            tr[i] = max(
                b["h"] - b["l"],
                abs(b["h"] - bars[i-1]["c"]),
                abs(b["l"] - bars[i-1]["c"]),
            )
        if i == n - 1:
            out[i] = sum(tr[:n]) / n
        elif i >= n:
            out[i] = (out[i-1] * (n - 1) + tr[i]) / n
    return out

def stochastic_8_3(bars):
    k = [math.nan] * len(bars)
    d = [math.nan] * len(bars)
    for i in range(7, len(bars)):
        w = bars[i-7:i+1]
        hh = max(x["h"] for x in w)
        ll = min(x["l"] for x in w)
        k[i] = 50.0 if hh == ll else 100.0 * (bars[i]["c"] - ll) / (hh - ll)
    for i in range(9, len(bars)):
        d[i] = (k[i] + k[i-1] + k[i-2]) / 3.0
    return k, d


def ema_series(values, n):
    out = [math.nan] * len(values)
    if len(values) < n:
        return out
    seed = sum(values[:n]) / n
    out[n - 1] = seed
    alpha = 2.0 / (n + 1.0)
    for i in range(n, len(values)):
        out[i] = values[i] * alpha + out[i - 1] * (1.0 - alpha)
    return out


def rsi_series(values, n=14):
    out = [math.nan] * len(values)
    if len(values) <= n:
        return out
    gains = [0.0] * len(values)
    losses = [0.0] * len(values)
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    avg_gain = sum(gains[1:n + 1]) / n
    avg_loss = sum(losses[1:n + 1]) / n
    out[n] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(n + 1, len(values)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def adx_series(bars, n=14):
    size = len(bars)
    adx = [math.nan] * size
    if size <= n * 2:
        return adx

    tr = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size
    for i in range(1, size):
        up = bars[i]["h"] - bars[i - 1]["h"]
        down = bars[i - 1]["l"] - bars[i]["l"]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(
            bars[i]["h"] - bars[i]["l"],
            abs(bars[i]["h"] - bars[i - 1]["c"]),
            abs(bars[i]["l"] - bars[i - 1]["c"]),
        )

    sm_tr = sum(tr[1:n + 1])
    sm_plus = sum(plus_dm[1:n + 1])
    sm_minus = sum(minus_dm[1:n + 1])
    dx = [math.nan] * size

    for i in range(n, size):
        if i > n:
            sm_tr = sm_tr - sm_tr / n + tr[i]
            sm_plus = sm_plus - sm_plus / n + plus_dm[i]
            sm_minus = sm_minus - sm_minus / n + minus_dm[i]
        if sm_tr <= 0:
            continue
        plus_di = 100.0 * sm_plus / sm_tr
        minus_di = 100.0 * sm_minus / sm_tr
        den = plus_di + minus_di
        dx[i] = 0.0 if den == 0 else 100.0 * abs(plus_di - minus_di) / den

    vals = [dx[i] for i in range(n, min(size, n * 2)) if math.isfinite(dx[i])]
    if len(vals) == n:
        adx[n * 2 - 1] = sum(vals) / n
        for i in range(n * 2, size):
            if math.isfinite(dx[i]):
                adx[i] = (adx[i - 1] * (n - 1) + dx[i]) / n
    return adx


def previous_day_pivots(bars):
    if not bars:
        return None
    last_dt = datetime.fromtimestamp(bars[-1]["t"] / 1000, timezone.utc).date()
    prior_dates = sorted({
        datetime.fromtimestamp(x["t"] / 1000, timezone.utc).date()
        for x in bars
        if datetime.fromtimestamp(x["t"] / 1000, timezone.utc).date() < last_dt
    })
    if not prior_dates:
        return None
    day = prior_dates[-1]
    rows = [
        x for x in bars
        if datetime.fromtimestamp(x["t"] / 1000, timezone.utc).date() == day
    ]
    if not rows:
        return None
    h = max(x["h"] for x in rows)
    l = min(x["l"] for x in rows)
    c = rows[-1]["c"]
    p = (h + l + c) / 3.0
    return {
        "day_utc": str(day),
        "p": p,
        "r1": 2 * p - l,
        "s1": 2 * p - h,
        "r2": p + (h - l),
        "s2": p - (h - l),
    }


def observe_market(inst):
    b = candles(inst)
    if len(b) < 220:
        return {"error": "za mało świec do pełnej obserwacji"}

    closes = [x["c"] for x in b]
    vols = [x["v"] for x in b]
    i = len(b) - 1

    k, d = stochastic_8_3(b)
    a = atr(b)
    adx = adx_series(b)
    rsi = rsi_series(closes, 14)
    rsi_ma9 = math.nan
    recent_rsi = [x for x in rsi[max(0, i - 8):i + 1] if math.isfinite(x)]
    if len(recent_rsi) == 9:
        rsi_ma9 = sum(recent_rsi) / 9.0

    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    ema200 = ema_series(closes, 200)
    macd = [math.nan] * len(closes)
    for q in range(len(closes)):
        if math.isfinite(ema12[q]) and math.isfinite(ema26[q]):
            macd[q] = ema12[q] - ema26[q]
    macd_valid = [0.0 if not math.isfinite(x) else x for x in macd]
    macd_signal = ema_series(macd_valid, 9)
    macd_hist = (
        macd[i] - macd_signal[i]
        if math.isfinite(macd[i]) and math.isfinite(macd_signal[i])
        else math.nan
    )

    obv = [0.0] * len(b)
    cvd_proxy = [0.0] * len(b)
    for q in range(1, len(b)):
        if closes[q] > closes[q - 1]:
            obv[q] = obv[q - 1] + vols[q]
        elif closes[q] < closes[q - 1]:
            obv[q] = obv[q - 1] - vols[q]
        else:
            obv[q] = obv[q - 1]

        signed_v = vols[q] if b[q]["c"] > b[q]["o"] else -vols[q] if b[q]["c"] < b[q]["o"] else 0.0
        cvd_proxy[q] = cvd_proxy[q - 1] + signed_v

    w20 = closes[-20:]
    mean20 = sum(w20) / 20.0
    std20 = (sum((x - mean20) ** 2 for x in w20) / 20.0) ** 0.5
    bb_upper = mean20 + 2.0 * std20
    bb_lower = mean20 - 2.0 * std20
    dc_upper = max(x["h"] for x in b[-20:])
    dc_lower = min(x["l"] for x in b[-20:])
    vol_ma20 = sum(vols[-20:]) / 20.0
    piv = previous_day_pivots(b)
    support, resistance = nearest_levels(b, a, i)

    def relation(v, ref):
        if not (math.isfinite(v) and math.isfinite(ref)):
            return None
        if v > ref:
            return "ABOVE"
        if v < ref:
            return "BELOW"
        return "EQUAL"

    if closes[i] > bb_upper:
        bb_position = "ABOVE_UPPER"
    elif closes[i] < bb_lower:
        bb_position = "BELOW_LOWER"
    else:
        bb_position = "INSIDE"

    if rsi[i] >= 70:
        rsi_zone = "OVERBOUGHT"
    elif rsi[i] <= 30:
        rsi_zone = "OVERSOLD"
    else:
        rsi_zone = "NEUTRAL"

    stoch_zone = "OVERBOUGHT" if k[i] >= 80 else "OVERSOLD" if k[i] <= 20 else "NEUTRAL"
    adx_strength = "STRONG" if adx[i] >= 25 else "WEAK"
    candle_color = "GREEN" if b[i]["c"] > b[i]["o"] else "RED" if b[i]["c"] < b[i]["o"] else "DOJI"
    macd_side = "BULLISH" if math.isfinite(macd[i]) and math.isfinite(macd_signal[i]) and macd[i] > macd_signal[i] else "BEARISH"
    hist_trend = None
    if i > 0 and math.isfinite(macd_hist) and math.isfinite(macd[i - 1]) and math.isfinite(macd_signal[i - 1]):
        prev_hist = macd[i - 1] - macd_signal[i - 1]
        hist_trend = "RISING" if macd_hist > prev_hist else "FALLING" if macd_hist < prev_hist else "FLAT"

    pivot_position = None
    if piv:
        if closes[i] > piv["r1"]:
            pivot_position = "ABOVE_R1"
        elif closes[i] > piv["p"]:
            pivot_position = "P_TO_R1"
        elif closes[i] < piv["s1"]:
            pivot_position = "BELOW_S1"
        elif closes[i] < piv["p"]:
            pivot_position = "S1_TO_P"
        else:
            pivot_position = "AT_P"

    return {
        "bar_time": b[i]["t"],
        "price": closes[i],
        "candle": {
            "open": b[i]["o"], "high": b[i]["h"], "low": b[i]["l"], "close": b[i]["c"],
            "color": candle_color,
        },
        "volume": {
            "value": vols[i],
            "ma20": vol_ma20,
            "vs_ma20": "ABOVE" if vols[i] > vol_ma20 else "BELOW" if vols[i] < vol_ma20 else "EQUAL",
        },
        "rsi14": {
            "value": rsi[i],
            "zone": rsi_zone,
            "ma9": rsi_ma9 if math.isfinite(rsi_ma9) else None,
            "vs_ma9": relation(rsi[i], rsi_ma9),
        },
        "macd_12_26_9": {
            "line": macd[i] if math.isfinite(macd[i]) else None,
            "signal": macd_signal[i] if math.isfinite(macd_signal[i]) else None,
            "histogram": macd_hist if math.isfinite(macd_hist) else None,
            "position": macd_side,
            "histogram_trend": hist_trend,
        },
        "stochastic_8_3": {
            "k": k[i], "d": d[i],
            "position": "K_ABOVE_D" if k[i] > d[i] else "K_BELOW_D" if k[i] < d[i] else "EQUAL",
            "zone": stoch_zone,
        },
        "adx14": {
            "value": adx[i] if math.isfinite(adx[i]) else None,
            "strength": adx_strength if math.isfinite(adx[i]) else None,
        },
        "obv": {
            "value": obv[i],
            "trend_5": "RISING" if obv[i] > obv[max(0, i - 5)] else "FALLING" if obv[i] < obv[max(0, i - 5)] else "FLAT",
        },
        "ema200": {
            "value": ema200[i] if math.isfinite(ema200[i]) else None,
            "price_position": relation(closes[i], ema200[i]),
        },
        "bollinger_20_2": {
            "lower": bb_lower, "mid": mean20, "upper": bb_upper,
            "price_position": bb_position,
        },
        "donchian20": {
            "lower": dc_lower, "upper": dc_upper,
            "price_position": (
                "AT_UPPER" if closes[i] >= dc_upper else
                "AT_LOWER" if closes[i] <= dc_lower else
                "INSIDE"
            ),
        },
        "atr14": {
            "value": a[i],
            "pct_of_price": (a[i] / closes[i] * 100.0) if closes[i] else None,
        },
        "pivot_daily": {
            **(piv or {}),
            "price_position": pivot_position,
        } if piv else None,
        "cvd_proxy": {
            "value": cvd_proxy[i],
            "trend_5": "RISING" if cvd_proxy[i] > cvd_proxy[max(0, i - 5)] else "FALLING" if cvd_proxy[i] < cvd_proxy[max(0, i - 5)] else "FLAT",
            "note": "proxy z kierunku świecy 15m, nie prawdziwy trade-side CVD",
        },
        "support_resistance": {
            "support": support["price"] if support else None,
            "support_touches": support["touches"] if support else None,
            "resistance": resistance["price"] if resistance else None,
            "resistance_touches": resistance["touches"] if resistance else None,
        },
        "change8_pct": (closes[i] / closes[i - 7] - 1.0) * 100.0,
        "used_for_entry_decision": False,
    }


def nearest_levels(bars, atr_values, i):
    cur = bars[i]["c"]
    if not math.isfinite(atr_values[i]):
        return None, None
    tol = atr_values[i] * 0.35
    pivots = []
    start = max(3, i - 220)

    for j in range(start, i - 2):
        is_hi = True
        is_lo = True
        left = max(0, j - 3)
        right = min(i, j + 3)
        for q in range(left, right + 1):
            if q == j:
                continue
            if bars[j]["h"] <= bars[q]["h"]:
                is_hi = False
            if bars[j]["l"] >= bars[q]["l"]:
                is_lo = False
        if is_hi:
            pivots.append(("R", bars[j]["h"]))
        if is_lo:
            pivots.append(("S", bars[j]["l"]))

    def cluster(kind):
        xs = sorted(p for t, p in pivots if t == kind)
        groups = []
        for p in xs:
            best = None
            best_dist = float("inf")
            for g in groups:
                dist = abs(g["mean"] - p)
                if dist < tol and dist < best_dist:
                    best = g
                    best_dist = dist
            if best is None:
                groups.append({"mean": p, "values": [p]})
            else:
                best["values"].append(p)
                best["mean"] = sum(best["values"]) / len(best["values"])
        return [{"price": g["mean"], "touches": len(g["values"])} for g in groups]

    supports = sorted(
        (x for x in cluster("S") if x["price"] < cur),
        key=lambda x: x["price"],
        reverse=True,
    )
    resistances = sorted(
        (x for x in cluster("R") if x["price"] > cur),
        key=lambda x: x["price"],
    )
    return (supports[0] if supports else None, resistances[0] if resistances else None)

def current_direction(k, d):
    if not (math.isfinite(k) and math.isfinite(d)):
        return 0
    if k > d and k > 50:
        return 1
    if k < d and k < 50:
        return -1
    return 0

def backtest_current_setup(bars, k, d, direction, target_pct):
    i_now = len(bars) - 1
    k_now = k[i_now]
    wins = losses = unresolved = samples = 0

    for i in range(220, i_now - HORIZON_BARS):
        if i < 7:
            continue
        change8 = (bars[i]["c"] / bars[i-7]["c"] - 1.0) * 100.0

        if direction == 1:
            same = k[i] > d[i] and k[i] > 50 and change8 > 0
        else:
            same = k[i] < d[i] and k[i] < 50 and change8 < 0

        if not same or abs(k[i] - k_now) > 10:
            continue

        samples += 1
        entry = bars[i]["c"]
        tp = entry * (1 + target_pct / 100.0) if direction == 1 else entry * (1 - target_pct / 100.0)
        sl = entry * (1 - SL_PCT / 100.0) if direction == 1 else entry * (1 + SL_PCT / 100.0)

        result = None
        for j in range(i + 1, i + 1 + HORIZON_BARS):
            hit_tp = bars[j]["h"] >= tp if direction == 1 else bars[j]["l"] <= tp
            hit_sl = bars[j]["l"] <= sl if direction == 1 else bars[j]["h"] >= sl
            if hit_tp and hit_sl:
                result = 0
                break
            if hit_tp:
                result = 1
                break
            if hit_sl:
                result = 0
                break

        if result == 1:
            wins += 1
        elif result == 0:
            losses += 1
        else:
            unresolved += 1

    decided = wins + losses
    hit_rate = 100.0 * wins / decided if decided else None
    return {
        "samples": samples,
        "decided": decided,
        "wins": wins,
        "losses": losses,
        "unresolved": unresolved,
        "hit_rate": hit_rate,
    }

def fmt_price(x):
    if x is None:
        return "-"
    if x >= 1:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    if x >= 0.01:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")

def analyse_candidate(inst):
    b = candles(inst)
    if len(b) < 300:
        return None

    k, d = stochastic_8_3(b)
    a = atr(b)
    i = len(b) - 1
    direction = current_direction(k[i], d[i])
    if direction == 0:
        return None

    change8 = (b[i]["c"] / b[i-7]["c"] - 1.0) * 100.0
    if direction == 1 and change8 <= 0:
        return None
    if direction == -1 and change8 >= 0:
        return None

    support, resistance = nearest_levels(b, a, i)
    target_price = None
    target_pct = None

    if direction == 1 and resistance:
        target_price = resistance["price"]
        target_pct = (target_price - b[i]["c"]) / b[i]["c"] * 100.0
    elif direction == -1 and support:
        target_price = support["price"]
        target_pct = (b[i]["c"] - target_price) / b[i]["c"] * 100.0

    bt = None
    if target_pct is not None and target_pct > 0:
        bt = backtest_current_setup(b, k, d, direction, target_pct)

    reasons = []
    if target_pct is None:
        reasons.append("brak najbliższego celu S/R")
    elif target_pct < MIN_TARGET_PCT:
        reasons.append(f"cel tylko {target_pct:.2f}% < {MIN_TARGET_PCT:.1f}%")

    if bt is None:
        reasons.append("brak backtestu")
    else:
        if bt["decided"] < MIN_DECIDED:
            reasons.append(f"tylko {bt['decided']} rozstrzygniętych przypadków")
        if bt["hit_rate"] is None or bt["hit_rate"] < MIN_HIT_RATE:
            hr = 0.0 if bt["hit_rate"] is None else bt["hit_rate"]
            reasons.append(f"skuteczność {hr:.1f}% < {MIN_HIT_RATE:.0f}%")

    passed = len(reasons) == 0

    return {
        "inst": inst,
        "direction": "LONG" if direction == 1 else "SHORT",
        "bar_time": b[i]["t"],
        "open": b[i]["o"],
        "high": b[i]["h"],
        "low": b[i]["l"],
        "close": b[i]["c"],
        "volume": b[i]["v"],
        "stoch_k_8": k[i],
        "stoch_d_3": d[i],
        "atr14": a[i],
        "change8_pct": change8,
        "support": support["price"] if support else None,
        "support_touches": support["touches"] if support else None,
        "resistance": resistance["price"] if resistance else None,
        "resistance_touches": resistance["touches"] if resistance else None,
        "k": k[i],
        "d": d[i],
        "change8": change8,
        "target_price": target_price,
        "target_pct": target_pct,
        "hit_rate": bt["hit_rate"] if bt else None,
        "wins": bt["wins"] if bt else 0,
        "losses": bt["losses"] if bt else 0,
        "decided": bt["decided"] if bt else 0,
        "passed": passed,
        "reasons": reasons,
    }


def decision_pros_cons(x):
    if not x:
        return [], ["brak pełnego sygnału kierunkowego po filtrze Stochastic + momentum"]

    pros = [
        f"Stochastic potwierdza {x['direction']} (K={x['stoch_k_8']:.2f}, D={x['stoch_d_3']:.2f})",
        f"momentum 8 świec zgodny z kierunkiem ({x['change8_pct']:.3f}%)",
    ]
    cons = list(x.get("reasons") or [])

    if x.get("target_pct") is not None and x["target_pct"] >= MIN_TARGET_PCT:
        pros.append(f"cel S/R wystarczająco daleko ({x['target_pct']:.2f}% >= {MIN_TARGET_PCT:.1f}%)")
    if x.get("decided", 0) >= MIN_DECIDED:
        pros.append(f"wystarczająca próba historyczna ({x['decided']} rozstrzygniętych)")
    if x.get("hit_rate") is not None and x["hit_rate"] >= MIN_HIT_RATE:
        pros.append(f"historyczna skuteczność {x['hit_rate']:.1f}% >= {MIN_HIT_RATE:.0f}%")

    return pros, cons


def write_decision_journal(closed_at, top, analysed_by_inst, observations_by_inst, selected):
    DECISION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    considered = []

    for row in top:
        x = analysed_by_inst.get(row["inst"])
        pros, cons = decision_pros_cons(x)
        if x is None:
            decision = "BRAK SYGNAŁU KIERUNKOWEGO"
        elif x.get("passed"):
            decision = "WEJŚCIE"
        else:
            decision = "NIE WCHODZIĆ"

        considered.append({
            "instrument": row["inst"],
            "change24_pct": row["change24"],
            "decision": decision,
            "pros": pros,
            "cons": cons,
            "analysis": x,
            "indicators_observed": observations_by_inst.get(row["inst"]),
        })

    closed_ms = int(closed_at.timestamp() * 1000)
    payload = {
        "closed_at_ms": closed_ms,
        "closed_at_uk": closed_at.isoformat(),
        "top10_market": top,
        "considered": considered,
        "selected_for_entry": selected,
    }

    path = DECISION_LOG_DIR / f"{closed_at:%Y%m%d_%H%M}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path

def main():
    top = top_usdt_swaps()
    candidates = []
    analysed_by_inst = {}
    observations_by_inst = {}
    errors = []

    for row in top:
        try:
            observations_by_inst[row["inst"]] = observe_market(row["inst"])
        except Exception as exc:
            observations_by_inst[row["inst"]] = {"error": str(exc)}

        try:
            result = analyse_candidate(row["inst"])
            analysed_by_inst[row["inst"]] = result
            if result:
                result["change24"] = row["change24"]
                candidates.append(result)
        except Exception as exc:
            analysed_by_inst[row["inst"]] = None
            errors.append(f'{row["inst"]}: {exc}')

    def rank(x):
        hr = x["hit_rate"] if x["hit_rate"] is not None else -1.0
        tp = x["target_pct"] if x["target_pct"] is not None else -1.0
        return (1 if x["passed"] else 0, hr, tp)

    longs = sorted((x for x in candidates if x["direction"] == "LONG"), key=rank, reverse=True)
    shorts = sorted((x for x in candidates if x["direction"] == "SHORT"), key=rank, reverse=True)

    now_uk = datetime.now(ZoneInfo("Europe/London"))
    minute = (now_uk.minute // 15) * 15
    closed_at = now_uk.replace(minute=minute, second=0, microsecond=0)

    passed_candidates = [x for x in candidates if x.get("passed")]
    passed_candidates.sort(
        key=lambda x: (
            float(x.get("hit_rate") or 0),
            float(x.get("target_pct") or 0),
            float(x.get("change24") or 0),
        ),
        reverse=True,
    )
    selected = passed_candidates[0] if passed_candidates else None

    snapshot = {
        "closed_at_uk": closed_at.isoformat(),
        "long": longs[0] if longs else None,
        "short": shorts[0] if shorts else None,
        "selected_for_entry": selected,
    }
    with open("blofin_scan_signal.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    journal_path = write_decision_journal(closed_at, top, analysed_by_inst, observations_by_inst, selected)

    print(f"## ŚWIECA 15m ZAMKNIĘTA — {closed_at:%H:%M} UK")
    print()

    def print_side(label, arrow, arr):
        if not arr:
            print(f"**{label} {arrow} — BRAK SYGNAŁU KIERUNKOWEGO**")
            return

        x = arr[0]
        status = "WEJŚCIE" if x["passed"] else "NIE WCHODZIĆ"
        print(f"## **{status}**")
        print(f"**{label} {arrow} {x['inst']}**")
        print(f"Wejście: **{fmt_price(x['close'])}**")
        if x["target_price"] is not None and x["target_pct"] is not None:
            print(f"Cel: **{fmt_price(x['target_price'])}** → ruch do celu **+{x['target_pct']:.2f}%**")
        else:
            if label == "LONG":
                print("Cel: **brak oporu powyżej ceny**")
            else:
                print("Cel: **brak wsparcia poniżej ceny**")
        if x["hit_rate"] is not None:
            print(f"Historyczna skuteczność: **{x['hit_rate']:.1f}%** ({x['wins']}/{x['decided']})")
        else:
            print("Historyczna skuteczność: **nie liczono — brak celu S/R**")
        if not x["passed"]:
            print("Powód odrzucenia: **" + "; ".join(x["reasons"]) + "**")

    print_side("LONG", "↑", longs)
    print()
    print_side("SHORT", "↓", shorts)

    print()
    print(f"Sygnały kierunkowe w TOP10: **LONG {len(longs)} / SHORT {len(shorts)}**")

    print()
    print(f"Pamiętnik decyzji: **{journal_path}**")

    if errors:
        print()
        print("Błędy częściowe: " + "; ".join(errors))

if __name__ == "__main__":
    main()
