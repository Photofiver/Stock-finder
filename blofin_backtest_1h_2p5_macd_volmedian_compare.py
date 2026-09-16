import bisect
import statistics
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
TOP_N = 10
NOTIONAL = 10.0
TP_PCT = 0.01
SL_PCT = 0.01
MAX_HOLD_MS = 60 * 60 * 1000
LOOKBACKS = [3, 5, 20]
SPIKE_CAP = 2.5
REQUEST_DELAY = 0.08
MAX_RETRIES = 6
D1H = 60 * 60 * 1000
D5M = 5 * 60 * 1000
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


def api_get(path, params=None):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(REQUEST_DELAY)
            r = requests.get(BASE + path, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            p = r.json()
            if str(p.get("code")) != "0":
                raise RuntimeError(p)
            return p.get("data", [])
        except Exception as exc:
            last = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed: {last}")


def parse(raw):
    out = []
    for row in raw:
        if len(row) < 9 or str(row[8]) != "1":
            continue
        try:
            out.append({"ts": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4]), "v": float(row[5])})
        except Exception:
            pass
    return sorted(out, key=lambda x: x["ts"])


def current_top10():
    live = set()
    for x in api_get("/api/v1/market/instruments"):
        if x.get("state") == "live" and x.get("instType") == "SWAP" and x.get("contractType") == "linear" and x.get("settleCurrency") == "USDT" and x.get("instId"):
            live.add(x["instId"])
    ranked = []
    for x in api_get("/api/v1/market/tickers"):
        inst = x.get("instId")
        if inst not in live:
            continue
        try:
            last = float(x.get("last") or 0)
            op = float(x.get("open24h") or 0)
            if last > 0 and op > 0:
                ranked.append(((last / op - 1) * 100, inst))
        except Exception:
            pass
    ranked.sort(reverse=True)
    return [{"inst": inst, "change": change, "rank": i + 1} for i, (change, inst) in enumerate(ranked[:TOP_N])]


def fetch_recent(inst, bar):
    return parse(api_get("/api/v1/market/candles", {"instId": inst, "bar": bar, "limit": "1440"}))


def ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for x in values[1:]:
        out.append(alpha * x + (1 - alpha) * out[-1])
    return out


def macd_hist(closes):
    fast = ema(closes, MACD_FAST)
    slow = ema(closes, MACD_SLOW)
    macd = [a - b for a, b in zip(fast, slow)]
    sig = ema(macd, MACD_SIGNAL)
    return [m - s for m, s in zip(macd, sig)]


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins = current_top10()
    print("CURRENT TOP10:", ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins))

    data = {}
    for c in coins:
        inst = c["inst"]
        h1 = fetch_recent(inst, "1H")
        m5 = fetch_recent(inst, "5m")
        hist = macd_hist([b["c"] for b in h1])
        for i, h in enumerate(hist):
            h1[i]["macd_hist"] = h
        data[inst] = {"1H": h1, "5m": m5}

    min_bars = MACD_SLOW + MACD_SIGNAL + 2
    available = [c["inst"] for c in coins if len(data[c["inst"]]["1H"]) >= min_bars and len(data[c["inst"]]["5m"]) >= 2]
    if not available:
        raise RuntimeError("No usable data")

    close1h = {inst: [b["ts"] + D1H for b in data[inst]["1H"]] for inst in available}
    close5 = {inst: [b["ts"] + D5M for b in data[inst]["5m"]] for inst in available}
    start = max(data[inst]["5m"][0]["ts"] + D5M for inst in available)
    end = min(data[inst]["5m"][-1]["ts"] + D5M for inst in available)

    def bar5(inst, t_ms):
        i = bisect.bisect_right(close5[inst], t_ms) - 1
        return data[inst]["5m"][i] if i >= 0 else None

    def signal(inst, t_ms, lookback):
        bars = data[inst]["1H"]
        i = bisect.bisect_right(close1h[inst], t_ms) - 1
        if i < max(lookback, min_bars - 1):
            return None, None, None
        prev, cur = bars[i - 1], bars[i]
        close_t = cur["ts"] + D1H
        if cur["v"] <= prev["v"]:
            return None, close_t, None
        prior = [b["v"] for b in bars[i - lookback:i]]
        med = statistics.median(prior)
        ratio = cur["v"] / med if med > 0 else float("inf")
        if ratio > SPIKE_CAP:
            return None, close_t, ratio
        hp, hc = prev["macd_hist"], cur["macd_hist"]
        if hp <= 0 < hc:
            return "LONG", close_t, ratio
        if hp >= 0 > hc:
            return "SHORT", close_t, ratio
        return None, close_t, ratio

    variants = {lb: {"open": None, "trades": [], "entries": 0} for lb in LOOKBACKS}

    t = start
    while t <= end:
        for lb, v in variants.items():
            pos = v["open"]
            if pos:
                b = bar5(pos["inst"], t)
                if b and b["ts"] + D5M > pos["entry_t"]:
                    if pos["side"] == "LONG":
                        hit_sl = b["l"] <= pos["sl"]
                        hit_tp = b["h"] >= pos["tp"]
                    else:
                        hit_sl = b["h"] >= pos["sl"]
                        hit_tp = b["l"] <= pos["tp"]
                    if hit_sl or hit_tp:
                        loss = hit_sl
                        v["trades"].append({**pos, "reason": "LOSS" if loss else "WIN", "pnl": -NOTIONAL * SL_PCT if loss else NOTIONAL * TP_PCT})
                        v["open"] = None
                if v["open"] and t - v["open"]["entry_t"] >= MAX_HOLD_MS:
                    p = v["open"]
                    b = bar5(p["inst"], t)
                    if b:
                        d = 1 if p["side"] == "LONG" else -1
                        v["trades"].append({**p, "reason": "TIME", "pnl": NOTIONAL * (b["c"] / p["entry"] - 1) * d})
                        v["open"] = None

            candidates = []
            for c in coins:
                inst = c["inst"]
                if inst not in available:
                    continue
                side, close_t, ratio = signal(inst, t, lb)
                if side is None or close_t != t:
                    continue
                b = bar5(inst, t)
                if b and b["c"] > 0:
                    candidates.append((-c["rank"], inst, side, b["c"], ratio))
            if v["open"] is None and candidates:
                candidates.sort(reverse=True, key=lambda x: x[0])
                _, inst, side, px, ratio = candidates[0]
                v["open"] = {
                    "inst": inst,
                    "side": side,
                    "entry_t": t,
                    "entry": px,
                    "ratio": ratio,
                    "tp": px * (1.01 if side == "LONG" else 0.99),
                    "sl": px * (0.99 if side == "LONG" else 1.01),
                }
                v["entries"] += 1
        t += D5M

    for v in variants.values():
        if v["open"]:
            p = v["open"]
            b = bar5(p["inst"], end)
            if b:
                d = 1 if p["side"] == "LONG" else -1
                v["trades"].append({**p, "reason": "END", "pnl": NOTIONAL * (b["c"] / p["entry"] - 1) * d})

    print("\n1H + MACD + 2.5x MEDIAN LOOKBACK COMPARISON")
    print("Rules: 1H volume>previous; volume<=2.5x median; MACD histogram red->green LONG / green->red SHORT; fresh 1H close")
    print("TP=1%, SL=1%, max hold=1h, one position at a time, TP/SL on 5m")
    print(f"window_utc={fmt(start)} -> {fmt(end)} days={(end-start)/86400000:.1f}")
    print("lookback | entries | wins | losses | time | end | winrate_tp_sl | gross_pnl")
    for lb in LOOKBACKS:
        v = variants[lb]
        wins = sum(x["reason"] == "WIN" for x in v["trades"])
        losses = sum(x["reason"] == "LOSS" for x in v["trades"])
        times = sum(x["reason"] == "TIME" for x in v["trades"])
        ends = sum(x["reason"] == "END" for x in v["trades"])
        total = sum(x["pnl"] for x in v["trades"])
        wl = wins + losses
        wr = 100 * wins / wl if wl else 0.0
        print(f"{lb} | {v['entries']} | {wins} | {losses} | {times} | {ends} | {wr:.2f}% | {total:+.4f} USDT")
    print("NOTE: same frozen current TOP10 and same fetched data for all lookbacks; fees/slippage excluded; both TP+SL in same 5m bar => LOSS.")


if __name__ == "__main__":
    main()
