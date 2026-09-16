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
    for c in coins:
        inst = c["inst"]
        bars = fetch_1h(inst)
        hist = macd_hist([b["c"] for b in bars])
        for i, h in enumerate(hist):
            bars[i]["macd_hist"] = h
        data[inst] = bars

    min_bars = MACD_SLOW + MACD_SIGNAL + 2
    available = [c["inst"] for c in coins if len(data[c["inst"]]) >= min_bars + 1]
    if not available:
        raise RuntimeError("No usable 1H data")

    start_idx = min_bars
    trades = []
    entries = 0

    # Only 1H data are used. Signal is read from a fully closed 1H candle.
    # Operationally this corresponds to checking at xx:01 after the xx:00 close.
    # Entry reference is the close of that just-closed 1H candle because no lower-TF data are used.
    max_len = min(len(data[inst]) for inst in available)

    for i in range(start_idx, max_len - 1):
        candidates = []
        for c in coins:
            inst = c["inst"]
            if inst not in available:
                continue
            bars = data[inst]
            prev, cur = bars[i - 1], bars[i]

            if cur["v"] <= prev["v"]:
                continue

            prior = [b["v"] for b in bars[i - VOL_MEDIAN_LOOKBACK:i]]
            med = statistics.median(prior)
            ratio = cur["v"] / med if med > 0 else float("inf")
            if ratio > SPIKE_CAP:
                continue

            hp = prev["macd_hist"]
            hc = cur["macd_hist"]
            if hp <= 0 < hc:
                side = "LONG"
            elif hp >= 0 > hc:
                side = "SHORT"
            else:
                continue

            candidates.append((-c["rank"], c, inst, side, cur["c"], ratio, cur["ts"] + D1H))

        if not candidates:
            continue

        candidates.sort(reverse=True, key=lambda x: x[0])
        _, c, inst, side, entry, ratio, signal_close_t = candidates[0]
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
            loss = hit_sl  # conservative if both are touched within same 1H candle
            reason = "LOSS" if loss else "WIN"
            pnl = -NOTIONAL * SL_PCT if loss else NOTIONAL * TP_PCT
        else:
            reason = "TIME"
            d = 1 if side == "LONG" else -1
            pnl = NOTIONAL * (nxt["c"] / entry - 1) * d

        trades.append({
            "inst": inst,
            "side": side,
            "signal_close_t": signal_close_t,
            "entry_check_t": signal_close_t + 60_000,
            "entry": entry,
            "ratio": ratio,
            "reason": reason,
            "pnl": pnl,
        })
        entries += 1

    wins = sum(t["reason"] == "WIN" for t in trades)
    losses = sum(t["reason"] == "LOSS" for t in trades)
    times = sum(t["reason"] == "TIME" for t in trades)
    total = sum(t["pnl"] for t in trades)
    wl = wins + losses
    wr = 100 * wins / wl if wl else 0.0

    first_t = min(data[inst][start_idx]["ts"] + D1H for inst in available)
    last_t = min(data[inst][max_len - 1]["ts"] + D1H for inst in available)

    print("\n1H-ONLY CLOSED-CANDLE BACKTEST")
    print("Rules: only 1H; evaluate at xx:01 after candle close; volume>previous; volume<=2.5x median(previous 3); MACD histogram red->green LONG / green->red SHORT")
    print("TP=1%, SL=1%, max hold=1h; exits evaluated ONLY from the next 1H candle; no 5m/15m/4H data used")
    print(f"window_utc={fmt(first_t)} -> {fmt(last_t)} days={(last_t-first_t)/86400000:.1f}")
    print(f"entries={entries} closed={len(trades)} wins={wins} losses={losses} time_exits={times}")
    print(f"tp_sl_win_rate={wr:.2f}% ({wins}/{wl})")
    print(f"gross_pnl={total:+.4f} USDT on max {NOTIONAL:.0f} USDT notional")
    print("NOTE: current TOP10 held fixed historically; fees/slippage excluded; if TP and SL both touch in the same next 1H candle, LOSS is assumed; entry price uses the closed 1H candle close because lower-TF data are intentionally excluded.")


if __name__ == "__main__":
    main()
