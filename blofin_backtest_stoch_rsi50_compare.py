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
SL_PCT = 0.01
HOLD_HOURS = 2
SPIKE_CAP = 2.5
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
RSI_PERIOD = 14
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
            out.append({"ts": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4]), "v": float(row[5])})
        except Exception:
            pass
    return sorted(out, key=lambda x: x["ts"])


def fetch_1h(inst):
    return parse(api_get("/api/v1/market/candles", {"instId": inst, "bar": "1H", "limit": "1440"}))


def fetch_1m_window(inst, entry_t):
    end_ts = entry_t + HOLD_HOURS * D1H
    raw = api_get("/api/v1/market/candles", {"instId": inst, "bar": "1m", "after": str(end_ts), "limit": "180"})
    bars = parse(raw)
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
    return sma(raw_k, STOCH_K_SMOOTH), sma(sma(raw_k, STOCH_K_SMOOTH), STOCH_D_PERIOD)


def rsi_wilder(bars, period=14):
    out = [None] * len(bars)
    if len(bars) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        d = bars[i]["c"] - bars[i - 1]["c"]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(bars)):
        d = bars[i]["c"] - bars[i - 1]["c"]
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    data = {}
    idx_by_close = {}
    for inst in SYMBOLS:
        bars = fetch_1h(inst)
        k, d = stochastic(bars)
        r = rsi_wilder(bars, RSI_PERIOD)
        for i, b in enumerate(bars):
            b["k"], b["d"], b["rsi"] = k[i], d[i], r[i]
        data[inst] = bars
        idx_by_close[inst] = {b["ts"] + D1H: i for i, b in enumerate(bars)}

    def candidates(t, require_rsi):
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
            if require_rsi:
                if prev["rsi"] is None or cur["rsi"] is None:
                    continue
                if side == "LONG" and not (prev["rsi"] <= 50.0 < cur["rsi"]):
                    continue
                if side == "SHORT" and not (prev["rsi"] >= 50.0 > cur["rsi"]):
                    continue
            out.append((RANK[inst], inst, side, cur["c"], prev.get("rsi"), cur.get("rsi")))
        out.sort(key=lambda x: x[0])
        return out

    cache = {}
    def resolve(inst, side, entry_t, entry):
        key = (inst, entry_t)
        if key not in cache:
            cache[key] = fetch_1m_window(inst, entry_t)
        bars = cache[key]
        tp = entry * (1 + TP_PCT if side == "LONG" else 1 - TP_PCT)
        sl = entry * (1 - SL_PCT if side == "LONG" else 1 + SL_PCT)
        for b in bars:
            if side == "LONG":
                ht, hs = b["h"] >= tp, b["l"] <= sl
            else:
                ht, hs = b["l"] <= tp, b["h"] >= sl
            if ht and hs:
                return "SAME_1M", b["ts"], None
            if ht:
                return "WIN", b["ts"], tp
            if hs:
                return "LOSS", b["ts"], sl
        exit_t = entry_t + HOLD_HOURS * D1H
        exit_px = bars[-1]["c"] if bars else entry
        return "TIME", exit_t, exit_px

    def simulate(require_rsi):
        trades = []
        busy_until = None
        t = START_TS
        raw_signals = 0
        while t <= END_TS:
            cs = candidates(t, require_rsi)
            if cs:
                raw_signals += 1
            if busy_until is not None and busy_until > t:
                t += D1H
                continue
            if cs:
                _, inst, side, entry, rp, rc = cs[0]
                reason, exit_t, exit_px = resolve(inst, side, t, entry)
                trades.append({"reason": reason, "side": side, "entry": entry, "exit": exit_px, "rsi_prev": rp, "rsi_cur": rc})
                busy_until = exit_t
            t += D1H

        wins = sum(x["reason"] == "WIN" for x in trades)
        losses = sum(x["reason"] == "LOSS" for x in trades)
        times = sum(x["reason"] == "TIME" for x in trades)
        same = sum(x["reason"] == "SAME_1M" for x in trades)
        pnl = 0.0
        for x in trades:
            if x["reason"] == "WIN": pnl += NOTIONAL * TP_PCT
            elif x["reason"] == "LOSS": pnl -= NOTIONAL * SL_PCT
            elif x["reason"] == "TIME":
                if x["side"] == "LONG": pnl += NOTIONAL * (x["exit"] / x["entry"] - 1)
                else: pnl += NOTIONAL * (1 - x["exit"] / x["entry"])
        resolved = wins + losses
        return {"trades": len(trades), "wins": wins, "losses": losses, "time": times, "same": same,
                "wr_all": 100*wins/len(trades) if trades else 0.0,
                "wr_res": 100*wins/resolved if resolved else 0.0,
                "pnl": pnl, "raw_signals": raw_signals}

    base = simulate(False)
    rsi = simulate(True)
    print(f"window={fmt(START_TS)} -> {fmt(END_TS)}")
    print("COMMON: only 1H signals; xx:01; Stochastic 14,3,3 cross; candle color agrees; volume>previous; volume<=2.5x median(prev3); TP=0.5%; SL=1%; max hold=2h; one position at a time; 1m only resolves exit order")
    print(f"BASE: trades={base['trades']} wins={base['wins']} losses={base['losses']} time={base['time']} same1m={base['same']} win_all={base['wr_all']:.2f}% win_tp_sl={base['wr_res']:.2f}% pnl={base['pnl']:+.4f} raw_signal_hours={base['raw_signals']}")
    print("RSI_FILTER: require same closed 1H candle RSI(14) cross 50: LONG prev<=50 and cur>50; SHORT prev>=50 and cur<50")
    print(f"STOCH_PLUS_RSI50: trades={rsi['trades']} wins={rsi['wins']} losses={rsi['losses']} time={rsi['time']} same1m={rsi['same']} win_all={rsi['wr_all']:.2f}% win_tp_sl={rsi['wr_res']:.2f}% pnl={rsi['pnl']:+.4f} raw_signal_hours={rsi['raw_signals']}")
    print(f"DELTA_WIN_ALL={rsi['wr_all']-base['wr_all']:+.2f}pp DELTA_PNL={rsi['pnl']-base['pnl']:+.4f} USDT")
    print("fees_slippage_excluded=true")


if __name__ == "__main__":
    main()
