import statistics
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
SYMBOLS = [
    "ARB-USDT", "CNPY-USDT", "USELESS-USDT", "MARSCOIN-USDT", "IOST-USDT",
    "ZEC-USDT", "FOLKS-USDT", "ZIL-USDT", "GRIFFAIN-USDT", "H-USDT",
]
RANK = {s: i + 1 for i, s in enumerate(SYMBOLS)}
NOTIONAL = 10.0
TP_PCT = 0.005
SL_PCT = 0.005
HOLD_HOURS = 2
SPIKE_CAP = 2.5
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
D1M = 60_000
D1H = 60 * D1M
START_TS = int(datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
END_TS = int(datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
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
            out.append({
                "ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5]),
            })
        except Exception:
            pass
    return sorted(out, key=lambda x: x["ts"])


def fetch_1h(inst):
    return parse(api_get("/api/v1/market/candles", {"instId": inst, "bar": "1H", "limit": "1440"}))


def fetch_1m_window(inst, entry_t):
    end_ts = entry_t + HOLD_HOURS * D1H
    raw = api_get("/api/v1/market/candles", {
        "instId": inst,
        "bar": "1m",
        "after": str(end_ts),
        "limit": "180",
    })
    bars = parse(raw)
    # Strategy acts at xx:01 after the 1H candle closes; exclude xx:00-xx:01.
    return [b for b in bars if entry_t + D1M <= b["ts"] < end_ts]


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        if any(v is None for v in w):
            continue
        out[i] = sum(w) / period
    return out


def stochastic(bars):
    raw_k = [None] * len(bars)
    for i in range(STOCH_K_PERIOD - 1, len(bars)):
        w = bars[i - STOCH_K_PERIOD + 1:i + 1]
        hh = max(b["h"] for b in w)
        ll = min(b["l"] for b in w)
        raw_k[i] = 50.0 if hh == ll else 100.0 * (bars[i]["c"] - ll) / (hh - ll)
    k = sma(raw_k, STOCH_K_SMOOTH)
    d = sma(k, STOCH_D_PERIOD)
    return k, d


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def resolve_trade(inst, side, entry_t, entry):
    tp = entry * (1 + TP_PCT if side == "LONG" else 1 - TP_PCT)
    sl = entry * (1 - SL_PCT if side == "LONG" else 1 + SL_PCT)
    bars = fetch_1m_window(inst, entry_t)

    for b in bars:
        if side == "LONG":
            hit_tp = b["h"] >= tp
            hit_sl = b["l"] <= sl
        else:
            hit_tp = b["l"] <= tp
            hit_sl = b["h"] >= sl
        if hit_tp and hit_sl:
            return {"reason": "SAME_1M", "exit_ts": b["ts"], "exit_price": None, "tp": tp, "sl": sl}
        if hit_tp:
            return {"reason": "WIN", "exit_ts": b["ts"], "exit_price": tp, "tp": tp, "sl": sl}
        if hit_sl:
            return {"reason": "LOSS", "exit_ts": b["ts"], "exit_price": sl, "tp": tp, "sl": sl}

    exit_ts = entry_t + HOLD_HOURS * D1H
    exit_price = bars[-1]["c"] if bars else entry
    return {"reason": "TIME", "exit_ts": exit_ts, "exit_price": exit_price, "tp": tp, "sl": sl}


def main():
    data = {}
    idx_by_close = {}
    for inst in SYMBOLS:
        bars = fetch_1h(inst)
        k, d = stochastic(bars)
        for i, b in enumerate(bars):
            b["k"], b["d"] = k[i], d[i]
        data[inst] = bars
        idx_by_close[inst] = {b["ts"] + D1H: i for i, b in enumerate(bars)}

    def candidates(t):
        out = []
        for inst in SYMBOLS:
            i = idx_by_close[inst].get(t)
            if i is None or i < 22:
                continue
            bars = data[inst]
            prev, cur = bars[i - 1], bars[i]
            if cur["v"] <= prev["v"]:
                continue
            med = statistics.median([b["v"] for b in bars[i - 3:i]])
            if med <= 0 or cur["v"] / med > SPIKE_CAP:
                continue
            if None in (prev["k"], prev["d"], cur["k"], cur["d"]):
                continue
            if prev["k"] <= prev["d"] and cur["k"] > cur["d"]:
                side = "LONG"
            elif prev["k"] >= prev["d"] and cur["k"] < cur["d"]:
                side = "SHORT"
            else:
                continue
            if side == "LONG" and cur["c"] <= cur["o"]:
                continue
            if side == "SHORT" and cur["c"] >= cur["o"]:
                continue
            out.append((RANK[inst], inst, side, cur["c"]))
        out.sort(key=lambda x: x[0])
        return out

    trades = []
    open_until = None
    t = START_TS
    while t <= END_TS:
        if open_until is not None and open_until > t:
            t += D1H
            continue
        cs = candidates(t)
        if cs:
            _, inst, side, entry = cs[0]
            res = resolve_trade(inst, side, t, entry)
            trade = {"entry_t": t, "inst": inst, "side": side, "entry": entry, **res}
            trades.append(trade)
            open_until = res["exit_ts"]
        t += D1H

    wins = [x for x in trades if x["reason"] == "WIN"]
    losses = [x for x in trades if x["reason"] == "LOSS"]
    times = [x for x in trades if x["reason"] == "TIME"]
    same = [x for x in trades if x["reason"] == "SAME_1M"]

    gross_known = 0.0
    for x in trades:
        if x["reason"] == "WIN":
            gross_known += NOTIONAL * TP_PCT
        elif x["reason"] == "LOSS":
            gross_known -= NOTIONAL * SL_PCT
        elif x["reason"] == "TIME":
            if x["side"] == "LONG":
                gross_known += NOTIONAL * (x["exit_price"] / x["entry"] - 1)
            else:
                gross_known += NOTIONAL * (1 - x["exit_price"] / x["entry"])

    resolved = len(wins) + len(losses)
    wr_resolved = 100.0 * len(wins) / resolved if resolved else 0.0
    wr_all_known = 100.0 * len(wins) / len(trades) if trades else 0.0
    min_wins = len(wins)
    max_wins = len(wins) + len(same)
    wr_all_min = 100.0 * min_wins / len(trades) if trades else 0.0
    wr_all_max = 100.0 * max_wins / len(trades) if trades else 0.0

    print("TP0.5_SL0.5_1M_RESOLVED")
    print(f"window={fmt(START_TS)} -> {fmt(END_TS)}")
    print(f"trades={len(trades)} wins={len(wins)} losses={len(losses)} time={len(times)} same_1m={len(same)}")
    print(f"resolved_tp_sl_win_rate={wr_resolved:.2f}%")
    print(f"wins_over_all_trades_known_only={wr_all_known:.2f}%")
    print(f"all_trade_win_rate_range_if_same1m={'%.2f' % wr_all_min}%..{'%.2f' % wr_all_max}%")
    print(f"gross_known_excluding_same1m={gross_known:+.4f} USDT")
    print("fees_slippage_excluded=true")
    if same:
        print("SAME_1M_DETAILS")
        for x in same:
            print(f"{fmt(x['entry_t'])} {x['inst']} {x['side']} same_minute={fmt(x['exit_ts'])}")


if __name__ == "__main__":
    main()
