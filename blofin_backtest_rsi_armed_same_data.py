import statistics
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
SYMBOLS = [
    "ARB-USDT", "CNPY-USDT", "USELESS-USDT", "IOST-USDT", "H-USDT",
    "DGAI-USDT", "ZEC-USDT", "QCOM-USDT", "FOLKS-USDT", "ZIL-USDT",
]
RANK = {s: i + 1 for i, s in enumerate(SYMBOLS)}
NOTIONAL = 10.0
TP_PCT = 0.005
SL_PCT = 0.01
HOLD_HOURS = 2
VOL_LOOKBACK = 3
SPIKE_CAP = 2.5
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
RSI_PERIOD = 14
D1M = 60_000
D1H = 60 * D1M
START_TS = int(datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
END_TS = int(datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
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


def fetch_1m(inst, end_ts, limit=180):
    return parse(api_get("/api/v1/market/candles", {
        "instId": inst, "bar": "1m", "after": str(end_ts), "limit": str(limit)
    }))


def sma(vals, period):
    out = [None] * len(vals)
    for i in range(period - 1, len(vals)):
        w = vals[i - period + 1:i + 1]
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


def rsi_wilder(bars, period=14):
    out = [None] * len(bars)
    if len(bars) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = bars[i]["c"] - bars[i - 1]["c"]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    out[period] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(period + 1, len(bars)):
        ch = bars[i]["c"] - bars[i - 1]["c"]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    data = {}
    idx_by_close = {}
    for inst in SYMBOLS:
        bars = fetch_1h(inst)
        k, d = stochastic(bars)
        rsi = rsi_wilder(bars, RSI_PERIOD)
        for i, b in enumerate(bars):
            b["k"], b["d"], b["rsi"] = k[i], d[i], rsi[i]
        data[inst] = bars
        idx_by_close[inst] = {b["ts"] + D1H: i for i, b in enumerate(bars)}

    cache_1m = {}
    def get_minutes(inst, entry_t):
        key = (inst, entry_t)
        if key not in cache_1m:
            end_t = entry_t + HOLD_HOURS * D1H
            bars = fetch_1m(inst, end_t, 180)
            cache_1m[key] = [b for b in bars if entry_t + D1M <= b["ts"] < end_t]
        return cache_1m[key]

    def volume_ok(bars, i):
        cur, prev = bars[i], bars[i - 1]
        if cur["v"] <= prev["v"]:
            return False
        med = statistics.median([b["v"] for b in bars[i - VOL_LOOKBACK:i]])
        return med > 0 and cur["v"] / med <= SPIKE_CAP

    def stoch_side(bars, i):
        prev, cur = bars[i - 1], bars[i]
        if None in (prev["k"], prev["d"], cur["k"], cur["d"]):
            return None
        if prev["k"] <= prev["d"] and cur["k"] > cur["d"]:
            side = "LONG"
        elif prev["k"] >= prev["d"] and cur["k"] < cur["d"]:
            side = "SHORT"
        else:
            return None
        if side == "LONG" and cur["c"] <= cur["o"]:
            return None
        if side == "SHORT" and cur["c"] >= cur["o"]:
            return None
        if not volume_ok(bars, i):
            return None
        return side

    def rsi_cross(bars, i):
        prev, cur = bars[i - 1], bars[i]
        rp, rc = prev["rsi"], cur["rsi"]
        if rp is None or rc is None:
            return None
        if rp <= 50 < rc:
            return "LONG"
        if rp >= 50 > rc:
            return "SHORT"
        return None

    def resolve(inst, side, entry_t, entry_px):
        bars = get_minutes(inst, entry_t)
        tp = entry_px * (1 + TP_PCT if side == "LONG" else 1 - TP_PCT)
        sl = entry_px * (1 - SL_PCT if side == "LONG" else 1 + SL_PCT)
        for b in bars:
            if side == "LONG":
                ht, hs = b["h"] >= tp, b["l"] <= sl
            else:
                ht, hs = b["l"] <= tp, b["h"] >= sl
            if ht and hs:
                return "LOSS", b["ts"], sl, True
            if ht:
                return "WIN", b["ts"], tp, False
            if hs:
                return "LOSS", b["ts"], sl, False
        exit_t = entry_t + HOLD_HOURS * D1H
        exit_px = bars[-1]["c"] if bars else entry_px
        return "TIME", exit_t, exit_px, False

    def summarize(name, trades, raw_hours, blocked_same_coin=0):
        wins = sum(x["reason"] == "WIN" for x in trades)
        losses = sum(x["reason"] == "LOSS" for x in trades)
        times = sum(x["reason"] == "TIME" for x in trades)
        same1m = sum(x["same1m"] for x in trades)
        gross = 0.0
        for x in trades:
            if x["reason"] == "WIN":
                gross += NOTIONAL * TP_PCT
            elif x["reason"] == "LOSS":
                gross -= NOTIONAL * SL_PCT
            else:
                if x["side"] == "LONG":
                    gross += NOTIONAL * (x["exit_px"] / x["entry_px"] - 1)
                else:
                    gross += NOTIONAL * (1 - x["exit_px"] / x["entry_px"])
        wl = wins + losses
        print(f"{name}: trades={len(trades)} wins={wins} losses={losses} time={times} same1m={same1m} "
              f"win_all={100*wins/len(trades) if trades else 0:.2f}% "
              f"win_tp_sl={100*wins/wl if wl else 0:.2f}% pnl={gross:+.4f} "
              f"raw_signal_hours={raw_hours} blocked_same_coin={blocked_same_coin}")

    # Baseline: Stochastic and RSI50 must cross on the same closed 1H candle.
    baseline = []
    busy_until = -1
    raw = 0
    t = START_TS
    while t <= END_TS:
        cs = []
        for inst in SYMBOLS:
            i = idx_by_close[inst].get(t)
            if i is None or i < 30:
                continue
            ss = stoch_side(data[inst], i)
            rs = rsi_cross(data[inst], i)
            if ss and rs == ss:
                cs.append((RANK[inst], inst, ss, data[inst][i]["c"]))
        cs.sort()
        if cs:
            raw += 1
        if cs and t >= busy_until:
            _, inst, side, entry_px = cs[0]
            reason, exit_t, exit_px, same = resolve(inst, side, t, entry_px)
            baseline.append({"inst": inst, "side": side, "entry_px": entry_px,
                             "exit_px": exit_px, "reason": reason, "same1m": same})
            busy_until = exit_t
        t += D1H

    # New rule: each RSI50 cross arms that coin in that direction. One Stochastic entry is allowed
    # while armed. After an entry, that coin stays blocked until its NEXT RSI50 cross.
    armed = {inst: None for inst in SYMBOLS}
    used = {inst: False for inst in SYMBOLS}
    armed_trades = []
    busy_until = -1
    raw_armed = 0
    blocked_same_coin = 0
    t = START_TS
    while t <= END_TS:
        # First update all RSI arms on this newly closed 1H candle.
        for inst in SYMBOLS:
            i = idx_by_close[inst].get(t)
            if i is None or i < 30:
                continue
            cross = rsi_cross(data[inst], i)
            if cross:
                armed[inst] = cross
                used[inst] = False

        cs = []
        for inst in SYMBOLS:
            i = idx_by_close[inst].get(t)
            if i is None or i < 30:
                continue
            ss = stoch_side(data[inst], i)
            if not ss:
                continue
            if armed[inst] == ss:
                if used[inst]:
                    blocked_same_coin += 1
                    continue
                cs.append((RANK[inst], inst, ss, data[inst][i]["c"]))
        cs.sort()
        if cs:
            raw_armed += 1
        if cs and t >= busy_until:
            _, inst, side, entry_px = cs[0]
            reason, exit_t, exit_px, same = resolve(inst, side, t, entry_px)
            armed_trades.append({"inst": inst, "side": side, "entry_px": entry_px,
                                 "exit_px": exit_px, "reason": reason, "same1m": same})
            used[inst] = True
            busy_until = exit_t
        t += D1H

    print("FROZEN_DATA=true")
    print("SYMBOLS=" + ",".join(SYMBOLS))
    print(f"WINDOW={fmt(START_TS)} -> {fmt(END_TS)} UTC")
    print("COMMON=1H Stochastic 14,3,3 + candle color + volume>previous + volume<=2.5x median(prev3); RSI14 cross 50; TP0.5%; SL1%; max2h; one global position; 1m only exit ordering")
    summarize("BASE_SAME_CANDLE_RSI50", baseline, raw)
    summarize("RSI_ARM_ONE_ENTRY_UNTIL_NEXT_CROSS", armed_trades, raw_armed, blocked_same_coin)
    print("fees_slippage_excluded=true")


if __name__ == "__main__":
    main()
