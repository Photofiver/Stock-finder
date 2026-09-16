import bisect
import statistics
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
TIMEFRAMES = ["4H", "1H", "15m", "5m"]
DURATION_MS = {"4H": 14400000, "1H": 3600000, "15m": 900000, "5m": 300000}
TOP_N = 10
NOTIONAL = 10.0
TP_PCT = 0.01
SL_PCT = 0.01
MAX_HOLD_MS = 3600000
VOL_MEDIAN_LOOKBACK = 3
SPIKE_CAP = 2.5
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
REQUEST_DELAY = 0.08
MAX_RETRIES = 6


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
    a = 2.0 / (period + 1.0)
    out = [values[0]]
    for x in values[1:]:
        out.append(a * x + (1.0 - a) * out[-1])
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
        data[inst] = {tf: fetch_recent(inst, tf) for tf in TIMEFRAMES}
        hist = macd_hist([b["c"] for b in data[inst]["1H"]])
        for i, h in enumerate(hist):
            data[inst]["1H"][i]["macd_hist"] = h

    min_1h = MACD_SLOW + MACD_SIGNAL + 2
    available = [c["inst"] for c in coins if all(len(data[c["inst"]][tf]) >= 2 for tf in TIMEFRAMES) and len(data[c["inst"]]["1H"]) >= min_1h]
    if not available:
        raise RuntimeError("No usable data")

    close_times = {inst: {tf: [b["ts"] + DURATION_MS[tf] for b in data[inst][tf]] for tf in TIMEFRAMES} for inst in available}
    start = max(data[inst]["5m"][0]["ts"] + DURATION_MS["5m"] for inst in available)
    end = min(data[inst]["5m"][-1]["ts"] + DURATION_MS["5m"] for inst in available)

    def idx_at(inst, tf, t_ms):
        return bisect.bisect_right(close_times[inst][tf], t_ms) - 1

    def tf_signal(inst, tf, t_ms):
        i = idx_at(inst, tf, t_ms)
        if i < 1:
            return None, None
        bars = data[inst][tf]
        prev, cur = bars[i - 1], bars[i]
        close_t = cur["ts"] + DURATION_MS[tf]
        if cur["v"] <= prev["v"]:
            return None, close_t
        if cur["c"] > cur["o"]:
            return "LONG", close_t
        if cur["c"] < cur["o"]:
            return "SHORT", close_t
        return None, close_t

    def h1_extra(inst, t_ms):
        i = idx_at(inst, "1H", t_ms)
        if i < max(VOL_MEDIAN_LOOKBACK, 1):
            return None, None, None
        bars = data[inst]["1H"]
        prev, cur = bars[i - 1], bars[i]
        close_t = cur["ts"] + DURATION_MS["1H"]
        if cur["v"] <= prev["v"]:
            return None, close_t, None
        prior = [b["v"] for b in bars[i - VOL_MEDIAN_LOOKBACK:i]]
        med = statistics.median(prior)
        ratio = cur["v"] / med if med > 0 else float("inf")
        if ratio > SPIKE_CAP:
            return None, close_t, ratio
        hp = prev["macd_hist"]
        hc = cur["macd_hist"]
        if hp <= 0 < hc:
            return "LONG", close_t, ratio
        if hp >= 0 > hc:
            return "SHORT", close_t, ratio
        return None, close_t, ratio

    def bar5(inst, t_ms):
        i = idx_at(inst, "5m", t_ms)
        return data[inst]["5m"][i] if i >= 0 else None

    open_pos = None
    trades = []
    entries = 0
    episodes = {inst: {"active": False, "used": False, "side": None} for inst in available}

    t = start
    step = DURATION_MS["5m"]
    while t <= end:
        if open_pos:
            b = bar5(open_pos["inst"], t)
            if b and b["ts"] + step > open_pos["entry_t"]:
                if open_pos["side"] == "LONG":
                    hit_sl, hit_tp = b["l"] <= open_pos["sl"], b["h"] >= open_pos["tp"]
                else:
                    hit_sl, hit_tp = b["h"] >= open_pos["sl"], b["l"] <= open_pos["tp"]
                if hit_sl or hit_tp:
                    loss = hit_sl
                    trades.append({**open_pos, "reason": "LOSS" if loss else "WIN", "pnl": -NOTIONAL * SL_PCT if loss else NOTIONAL * TP_PCT})
                    open_pos = None
            if open_pos and t - open_pos["entry_t"] >= MAX_HOLD_MS:
                p = open_pos
                b = bar5(p["inst"], t)
                if b:
                    d = 1 if p["side"] == "LONG" else -1
                    trades.append({**p, "reason": "TIME", "pnl": NOTIONAL * (b["c"] / p["entry"] - 1) * d})
                    open_pos = None

        candidates = []
        for c in coins:
            inst = c["inst"]
            if inst not in available:
                continue

            sigs = {tf: tf_signal(inst, tf, t) for tf in TIMEFRAMES}
            dirs = [sigs[tf][0] for tf in TIMEFRAMES]
            longs = sum(d == "LONG" for d in dirs)
            shorts = sum(d == "SHORT" for d in dirs)
            side = "LONG" if longs >= 3 and shorts == 0 else ("SHORT" if shorts >= 3 and longs == 0 else None)

            ep = episodes[inst]
            if side is None:
                ep["active"] = False
                ep["used"] = False
                ep["side"] = None
                continue
            if not ep["active"] or ep["side"] != side:
                ep["active"] = True
                ep["used"] = False
                ep["side"] = side

            # Preserve V04 freshness: only evaluate on a fresh 15m close.
            if sigs["15m"][1] != t:
                continue

            macd_side, h1_close_t, ratio = h1_extra(inst, t)
            if h1_close_t != t or macd_side != side:
                continue
            if ep["used"]:
                continue

            b = bar5(inst, t)
            if b and b["c"] > 0:
                matches = max(longs, shorts)
                candidates.append((matches, -c["rank"], c, inst, side, b["c"], ratio))

        if open_pos is None and candidates:
            candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
            matches, _, c, inst, side, px, ratio = candidates[0]
            open_pos = {
                "inst": inst,
                "side": side,
                "matches": matches,
                "entry_t": t,
                "entry": px,
                "vol_ratio": ratio,
                "tp": px * (1.01 if side == "LONG" else 0.99),
                "sl": px * (0.99 if side == "LONG" else 1.01),
            }
            entries += 1
            episodes[inst]["used"] = True
        t += step

    if open_pos:
        p = open_pos
        b = bar5(p["inst"], end)
        if b:
            d = 1 if p["side"] == "LONG" else -1
            trades.append({**p, "reason": "END", "pnl": NOTIONAL * (b["c"] / p["entry"] - 1) * d})

    wins = sum(x["reason"] == "WIN" for x in trades)
    losses = sum(x["reason"] == "LOSS" for x in trades)
    times = sum(x["reason"] == "TIME" for x in trades)
    ends = sum(x["reason"] == "END" for x in trades)
    total = sum(x["pnl"] for x in trades)
    wl = wins + losses
    wr = 100 * wins / wl if wl else 0.0

    print("\n3/4 + 1H MACD + MEDIAN3 BACKTEST")
    print("Rules: >=3/4 same-direction volume signals, no opposite; fresh15; one episode; 1H MACD histogram cross same direction; 1H volume>previous and <=2.5x median(previous 3); TP=1%; SL=1%; max hold=1h")
    print(f"window_utc={fmt(start)} -> {fmt(end)} days={(end-start)/86400000:.1f}")
    print(f"entries={entries} closed={len(trades)} wins={wins} losses={losses} time_exits={times} end_exits={ends}")
    print(f"tp_sl_win_rate={wr:.2f}% ({wins}/{wl})")
    print(f"gross_pnl={total:+.4f} USDT on max {NOTIONAL:.0f} USDT notional")
    print("NOTE: current TOP10 held fixed historically; fees/slippage excluded; TP/SL checked on 5m bars; if both touch in one bar, LOSS is assumed.")


if __name__ == "__main__":
    main()
