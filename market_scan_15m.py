#!/usr/bin/env python3
import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = "https://openapi.blofin.com"
TOP_N = 10
BAR = "15m"
CANDLE_LIMIT = 1440
MIN_TARGET_PCT = 1.0
MIN_HIT_RATE = 65.0
MIN_DECIDED = 30
HORIZON_BARS = 8
SL_PCT = 1.0

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

def analyse(inst):
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
    if direction == 1:
        if not resistance:
            return None
        target_price = resistance["price"]
        target_pct = (target_price - b[i]["c"]) / b[i]["c"] * 100.0
    else:
        if not support:
            return None
        target_price = support["price"]
        target_pct = (b[i]["c"] - target_price) / b[i]["c"] * 100.0

    if target_pct < MIN_TARGET_PCT:
        return None

    bt = backtest_current_setup(b, k, d, direction, target_pct)
    if bt["decided"] < MIN_DECIDED or bt["hit_rate"] is None or bt["hit_rate"] < MIN_HIT_RATE:
        return None

    support_pct = ((b[i]["c"] - support["price"]) / b[i]["c"] * 100.0) if support else None
    resistance_pct = ((resistance["price"] - b[i]["c"]) / b[i]["c"] * 100.0) if resistance else None

    return {
        "inst": inst,
        "direction": "LONG" if direction == 1 else "SHORT",
        "close": b[i]["c"],
        "bar_time": b[i]["t"],
        "k": k[i],
        "d": d[i],
        "change8": change8,
        "support": support["price"] if support else None,
        "support_pct": support_pct,
        "resistance": resistance["price"] if resistance else None,
        "resistance_pct": resistance_pct,
        "target_price": target_price,
        "target_pct": target_pct,
        "hit_rate": bt["hit_rate"],
        "wins": bt["wins"],
        "losses": bt["losses"],
        "decided": bt["decided"],
    }

def main():
    top = top_usdt_swaps()
    setups = []
    errors = []

    for row in top:
        try:
            result = analyse(row["inst"])
            if result:
                result["change24"] = row["change24"]
                setups.append(result)
        except Exception as exc:
            errors.append(f'{row["inst"]}: {exc}')

    setups.sort(key=lambda x: (x["hit_rate"], x["target_pct"]), reverse=True)

    now_uk = datetime.now(ZoneInfo("Europe/London"))
    print(f"## Skan BloFin 15m — {now_uk:%Y-%m-%d %H:%M} UK")
    print()
    print(f"Warunki wejścia: zamknięta świeca 15m, Stochastic(8,3) zgodny z kierunkiem 8 świec, cel do najbliższego wsparcia/oporu co najmniej {MIN_TARGET_PCT:.1f}%, historyczna skuteczność co najmniej {MIN_HIT_RATE:.0f}% na min. {MIN_DECIDED} rozstrzygniętych przypadkach.")
    print()

    if not setups:
        print("**BRAK WEJŚCIA**")
    else:
        for n, s in enumerate(setups, 1):
            print(f"### {n}. {s['inst']} — {s['direction']}")
            print(f"- Cena 15m: **{fmt_price(s['close'])}**")
            print(f"- Wsparcie: **{fmt_price(s['support'])}** ({-s['support_pct']:.2f}% od ceny)" if s["support_pct"] is not None else "- Wsparcie: -")
            print(f"- Opór: **{fmt_price(s['resistance'])}** (+{s['resistance_pct']:.2f}% od ceny)" if s["resistance_pct"] is not None else "- Opór: -")
            print(f"- Cel kierunkowy: **{fmt_price(s['target_price'])}** = **{s['target_pct']:.2f}%**")
            print(f"- Stochastic: K **{s['k']:.1f}**, D **{s['d']:.1f}**")
            print(f"- Backtest podobnego układu: **{s['hit_rate']:.1f}%** ({s['wins']}/{s['decided']})")
            print()

    print("---")
    print("To jest automatyczny skan techniczny, nie gwarancja wyniku.")
    if errors:
        print()
        print("Błędy częściowe: " + "; ".join(errors))

if __name__ == "__main__":
    main()
