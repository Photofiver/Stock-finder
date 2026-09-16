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
TP_PCT = 0.005
SL_PCT = 0.01
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


def fetch_1m_before(inst, end_ts):
    # BloFin 'after' returns candles earlier than the supplied timestamp.
    raw = api_get("/api/v1/market/candles", {
        "instId": inst,
        "bar": "1m",
        "after": str(end_ts),
        "limit": "180",
    })
    return parse(raw)


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
    return sma(raw_k, STOCH_K_SMOOTH), sma(sma(raw_k, STOCH_K_SMOOTH), STOCH_D_PERIOD)


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def build_both_touch_trades():
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

    open_pos = None
    trades = []
    t = START_TS
    while t <= END_TS:
        if open_pos is not None and t > open_pos["entry_t"]:
            inst = open_pos["inst"]
            i = idx_by_close[inst].get(t)
            if i is not None:
                b = data[inst][i]
                if open_pos["side"] == "LONG":
                    hit_sl = b["l"] <= open_pos["sl"]
                    hit_tp = b["h"] >= open_pos["tp"]
                else:
                    hit_sl = b["h"] >= open_pos["sl"]
                    hit_tp = b["l"] <= open_pos["tp"]
                if hit_sl or hit_tp:
                    trades.append({
                        **open_pos,
                        "exit_t": t,
                        "hours": int((t - open_pos["entry_t"]) / D1H),
                        "hit_tp": hit_tp,
                        "hit_sl": hit_sl,
                    })
                    open_pos = None
                elif t - open_pos["entry_t"] >= HOLD_HOURS * D1H:
                    open_pos = None

        if open_pos is None:
            cs = candidates(t)
            if cs:
                _, inst, side, entry = cs[0]
                open_pos = {
                    "inst": inst,
                    "side": side,
                    "entry_t": t,
                    "entry": entry,
                    "tp": entry * (1.005 if side == "LONG" else 0.995),
                    "sl": entry * (0.99 if side == "LONG" else 1.01),
                }
        t += D1H

    return [x for x in trades if x["hit_tp"] and x["hit_sl"]]


def resolve_one(x):
    bars = fetch_1m_before(x["inst"], x["exit_t"])
    candle_start = x["exit_t"] - D1H
    # Real rule checks the closed 1H signal at xx:01. For a first-hour exit,
    # ignore the xx:00-xx:01 minute because the position did not yet exist.
    live_start = max(candle_start, x["entry_t"] + D1M)
    bars = [b for b in bars if live_start <= b["ts"] < x["exit_t"]]

    for b in bars:
        if x["side"] == "LONG":
            tp_hit = b["h"] >= x["tp"]
            sl_hit = b["l"] <= x["sl"]
        else:
            tp_hit = b["l"] <= x["tp"]
            sl_hit = b["h"] >= x["sl"]
        if tp_hit and sl_hit:
            return "SAME_1M", b["ts"], len(bars)
        if tp_hit:
            return "TP_FIRST", b["ts"], len(bars)
        if sl_hit:
            return "SL_FIRST", b["ts"], len(bars)
    return "NO_TOUCH_AFTER_ENTRY", None, len(bars)


def main():
    both = build_both_touch_trades()
    print(f"REPRO_BOTH_TOUCH={len(both)} expected=23")
    counts = {"TP_FIRST": 0, "SL_FIRST": 0, "SAME_1M": 0, "NO_TOUCH_AFTER_ENTRY": 0}
    results = []
    for n, x in enumerate(both, 1):
        status, first_ts, bars_n = resolve_one(x)
        counts[status] += 1
        results.append((x, status, first_ts, bars_n))
        print(
            f"#{n:02d} entry={fmt(x['entry_t'])} {x['inst']} {x['side']} "
            f"exit_hour={x['hours']} first={status} "
            f"minute={fmt(first_ts) if first_ts else '-'} tp={x['tp']:.10g} sl={x['sl']:.10g} bars1m={bars_n}"
        )

    print("\nSUMMARY_1M")
    for key in ("TP_FIRST", "SL_FIRST", "SAME_1M", "NO_TOUCH_AFTER_ENTRY"):
        print(f"{key}={counts[key]}")
    print(f"RESOLVED_DIRECTIONAL={counts['TP_FIRST'] + counts['SL_FIRST']}/{len(both)}")
    print("NOTE: SAME_1M still cannot be ordered from 1-minute OHLC. The xx:00-xx:01 minute is excluded for first-hour exits because the strategy checks the 1H signal at xx:01.")


if __name__ == "__main__":
    main()
