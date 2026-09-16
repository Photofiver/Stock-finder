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
VOL_MEDIAN_LOOKBACK = 3
SPIKE_CAP = 2.5
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
REQUEST_DELAY = 0.08
MAX_RETRIES = 6
D1H = 60 * 60 * 1000


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
            out.append({
                "ts": int(row[0]),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            })
        except Exception:
            pass
    return sorted(out, key=lambda x: x["ts"])


def current_top10():
    live = set()
    for x in api_get("/api/v1/market/instruments"):
        if (
            x.get("state") == "live"
            and x.get("instType") == "SWAP"
            and x.get("contractType") == "linear"
            and x.get("settleCurrency") == "USDT"
            and x.get("instId")
        ):
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
    return [
        {"inst": inst, "change": change, "rank": i + 1}
        for i, (change, inst) in enumerate(ranked[:TOP_N])
    ]


def fetch_1h(inst):
    return parse(api_get("/api/v1/market/candles", {"instId": inst, "bar": "1H", "limit": "1440"}))


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
    close_times = {}
    for c in coins:
        inst = c["inst"]
        bars = fetch_1h(inst)
        hist = macd_hist([b["c"] for b in bars])
        for i, h in enumerate(hist):
            bars[i]["macd_hist"] = h
        data[inst] = bars
        close_times[inst] = [b["ts"] + D1H for b in bars]

    min_idx = MACD_SLOW + MACD_SIGNAL + 2
    available = [c["inst"] for c in coins if len(data[c["inst"]]) >= min_idx + 2]
    if not available:
        raise RuntimeError("No usable 1H data")

    # Align every symbol by the SAME absolute 1H close timestamp.
    start = max(close_times[inst][min_idx] for inst in available)
    end = min(close_times[inst][-2] for inst in available)  # need one full next 1H candle for exit

    trades = []
    entries = 0
    t = start

    while t <= end:
        candidates = []
        for c in coins:
            inst = c["inst"]
            if inst not in available:
                continue

            times = close_times[inst]
            i = bisect.bisect_left(times, t)
            if i >= len(times) or times[i] != t or i < min_idx or i + 1 >= len(data[inst]):
                continue

            bars = data[inst]
            prev, cur = bars[i - 1], bars[i]

            # Volume rule on the just-closed 1H candle.
            if cur["v"] <= prev["v"]:
                continue

            prior = [b["v"] for b in bars[i - VOL_MEDIAN_LOOKBACK:i]]
            med = statistics.median(prior)
            ratio = cur["v"] / med if med > 0 else float("inf")
            if ratio > SPIKE_CAP:
                continue

            # MACD histogram color change on the same closed 1H candle.
            hp = prev["macd_hist"]
            hc = cur["macd_hist"]
            if hp <= 0 < hc:
                side = "LONG"
            elif hp >= 0 > hc:
                side = "SHORT"
            else:
                continue

            candidates.append((-c["rank"], c, inst, side, cur["c"], ratio, i))

        if candidates:
            candidates.sort(reverse=True, key=lambda x: x[0])
            _, c, inst, side, entry, ratio, i = candidates[0]
            nxt = data[inst][i + 1]
            tp = entry * (1.01 if side == "LONG" else 0.99)
            sl = entry * (0.99 if side == "LONG" else 1.01)

            if side == "LONG":
                hit_sl = nxt["l"] <= sl
                hit_tp = nxt["h"] >= tp
            else:
                hit_sl = nxt["h"] >= sl
                hit_tp = nxt["l"] <= tp

            if hit_sl or hit_tp:
                loss = hit_sl  # conservative if both levels occur inside the same 1H candle
                reason = "LOSS" if loss else "WIN"
                pnl = -NOTIONAL * SL_PCT if loss else NOTIONAL * TP_PCT
            else:
                reason = "TIME"
                d = 1 if side == "LONG" else -1
                pnl = NOTIONAL * (nxt["c"] / entry - 1) * d

            trades.append({
                "inst": inst,
                "side": side,
                "signal_close_t": t,
                "check_t": t + 60_000,  # strategy is checked at xx:01
                "entry": entry,
                "ratio": ratio,
                "reason": reason,
                "pnl": pnl,
            })
            entries += 1

        t += D1H

    wins = sum(x["reason"] == "WIN" for x in trades)
    losses = sum(x["reason"] == "LOSS" for x in trades)
    times = sum(x["reason"] == "TIME" for x in trades)
    total = sum(x["pnl"] for x in trades)
    wl = wins + losses
    wr = 100 * wins / wl if wl else 0.0

    print("\n1H-ONLY CLOSED-CANDLE BACKTEST")
    print("Rules: ONLY 1H; evaluate at xx:01 after the 1H candle closes; volume>previous; volume<=2.5x median(previous 3); MACD histogram red->green LONG / green->red SHORT")
    print("TP=1%, SL=1%, max hold=1h; TP/SL/time exit evaluated ONLY from the next 1H candle; no 5m/15m/4H data used")
    print(f"window_utc={fmt(start)} -> {fmt(end)} days={(end-start)/86400000:.1f}")
    print(f"entries={entries} closed={len(trades)} wins={wins} losses={losses} time_exits={times}")
    print(f"tp_sl_win_rate={wr:.2f}% ({wins}/{wl})")
    print(f"gross_pnl={total:+.4f} USDT on max {NOTIONAL:.0f} USDT notional")
    print("NOTE: current TOP10 held fixed historically; fees/slippage excluded; if TP and SL both touch in the same next 1H candle, LOSS is assumed. Because lower-TF data are intentionally excluded, entry uses the close of the just-closed 1H candle as the price proxy for the xx:01 check.")


if __name__ == "__main__":
    main()
