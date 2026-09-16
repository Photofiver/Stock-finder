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
START_TS = int(datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
END_TS = int(datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)

NOTIONAL = 10.0
TP_PCT = 0.005
SL_PCT = 0.01
HOLD_MIN = 120
SPIKE_CAP = 2.5
VOL_LOOKBACK = 3
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
RSI_PERIOD = 14
D1M = 60_000
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


def fetch_1m(inst):
    # Seven days = 10080 bars; fetch extra warm-up and 2h exit tail.
    all_rows = {}
    target_oldest = START_TS - 240 * D1M
    cursor = END_TS + HOLD_MIN * D1M + D1M
    for _ in range(10):
        raw = api_get("/api/v1/market/candles", {
            "instId": inst, "bar": "1m", "after": str(cursor), "limit": "1440"
        })
        bars = parse(raw)
        if not bars:
            break
        for b in bars:
            all_rows[b["ts"]] = b
        oldest = min(b["ts"] for b in bars)
        if oldest <= target_oldest:
            break
        cursor = oldest
    return sorted(all_rows.values(), key=lambda x: x["ts"])


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


def volume_ok(bars, i):
    if i < VOL_LOOKBACK:
        return False
    cur, prev = bars[i], bars[i - 1]
    if cur["v"] <= prev["v"]:
        return False
    med = statistics.median([b["v"] for b in bars[i - VOL_LOOKBACK:i]])
    return med > 0 and cur["v"] / med <= SPIKE_CAP


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    data = {}
    by_close = {}
    ts_to_index = {}
    for inst in SYMBOLS:
        bars = fetch_1m(inst)
        k, d = stochastic(bars)
        rsi = rsi_wilder(bars, RSI_PERIOD)
        for i, b in enumerate(bars):
            b["k"], b["d"], b["rsi"] = k[i], d[i], rsi[i]
        data[inst] = bars
        by_close[inst] = {b["ts"] + D1M: i for i, b in enumerate(bars)}
        ts_to_index[inst] = {b["ts"]: i for i, b in enumerate(bars)}
        print(f"FETCHED {inst} 1m_bars={len(bars)}")

    armed = {s: None for s in SYMBOLS}
    used = {s: False for s in SYMBOLS}
    busy_until = -1
    trades = []
    raw_candidates = 0
    blocked_same_coin = 0
    rsi_crosses = 0
    min_idx = 30

    t = START_TS
    while t <= END_TS:
        candidates = []
        for inst in SYMBOLS:
            i = by_close[inst].get(t)
            if i is None or i < min_idx:
                continue
            bars = data[inst]
            prev, cur = bars[i - 1], bars[i]
            rp, rc = prev["rsi"], cur["rsi"]
            if rp is None or rc is None:
                continue

            cross_side = None
            if rp <= 50 < rc:
                cross_side = "LONG"
            elif rp >= 50 > rc:
                cross_side = "SHORT"
            if cross_side:
                armed[inst] = cross_side
                used[inst] = False
                rsi_crosses += 1

            if armed[inst] is None:
                continue
            if used[inst]:
                blocked_same_coin += 1
                continue
            if not volume_ok(bars, i):
                continue
            if None in (prev["k"], prev["d"], cur["k"], cur["d"]):
                continue

            stoch_side = None
            if prev["k"] <= prev["d"] and cur["k"] > cur["d"]:
                stoch_side = "LONG"
            elif prev["k"] >= prev["d"] and cur["k"] < cur["d"]:
                stoch_side = "SHORT"
            if stoch_side != armed[inst]:
                continue
            if stoch_side == "LONG" and cur["c"] <= cur["o"]:
                continue
            if stoch_side == "SHORT" and cur["c"] >= cur["o"]:
                continue
            candidates.append((RANK[inst], inst, stoch_side, cur["c"]))

        if candidates:
            raw_candidates += 1
        if t < busy_until or not candidates:
            t += D1M
            continue

        candidates.sort(key=lambda x: x[0])
        _, inst, side, entry_px = candidates[0]
        entry_t = t
        used[inst] = True

        bars = data[inst]
        start_i = ts_to_index[inst].get(entry_t)
        future = [] if start_i is None else [b for b in bars[start_i + 1:] if b["ts"] < entry_t + HOLD_MIN * D1M]
        tp = entry_px * (1 + TP_PCT if side == "LONG" else 1 - TP_PCT)
        sl = entry_px * (1 - SL_PCT if side == "LONG" else 1 + SL_PCT)
        reason = "TIME"
        exit_t = entry_t + HOLD_MIN * D1M
        exit_px = future[-1]["c"] if future else entry_px
        same1m = False

        for b in future:
            if side == "LONG":
                hit_tp, hit_sl = b["h"] >= tp, b["l"] <= sl
            else:
                hit_tp, hit_sl = b["l"] <= tp, b["h"] >= sl
            if hit_tp and hit_sl:
                reason = "LOSS"
                same1m = True
                exit_t, exit_px = b["ts"], sl
                break
            if hit_tp:
                reason = "WIN"
                exit_t, exit_px = b["ts"], tp
                break
            if hit_sl:
                reason = "LOSS"
                exit_t, exit_px = b["ts"], sl
                break

        trades.append({
            "inst": inst, "side": side, "entry": entry_px, "exit": exit_px,
            "entry_t": entry_t, "exit_t": exit_t, "reason": reason, "same1m": same1m,
        })
        busy_until = exit_t
        t += D1M

    wins = sum(x["reason"] == "WIN" for x in trades)
    losses = sum(x["reason"] == "LOSS" for x in trades)
    times = sum(x["reason"] == "TIME" for x in trades)
    same = sum(x["same1m"] for x in trades)
    gross = 0.0
    for x in trades:
        if x["reason"] == "WIN":
            gross += NOTIONAL * TP_PCT
        elif x["reason"] == "LOSS":
            gross -= NOTIONAL * SL_PCT
        else:
            if x["side"] == "LONG":
                gross += NOTIONAL * (x["exit"] / x["entry"] - 1)
            else:
                gross += NOTIONAL * (1 - x["exit"] / x["entry"])

    wl = wins + losses
    print("FROZEN_DATA=true")
    print("SYMBOLS=" + ",".join(SYMBOLS))
    print(f"WINDOW={fmt(START_TS)} -> {fmt(END_TS)} UTC")
    print("STRATEGY=1m RSI14 cross 50 arms coin; wait for same-direction Stochastic 14,3,3 cross; candle color agrees; volume>previous and <=2.5x median(prev3); one entry per coin until next RSI cross; one global position; TP0.5%; SL1%; max2h; same 1m dataset resolves exits")
    print(f"RSI_CROSSES={rsi_crosses} RAW_CANDIDATE_TIMES={raw_candidates} BLOCKED_SAME_COIN_CHECKS={blocked_same_coin}")
    print(f"RESULT: trades={len(trades)} wins={wins} losses={losses} time={times} same1m={same} win_all={100*wins/len(trades) if trades else 0:.2f}% win_tp_sl={100*wins/wl if wl else 0:.2f}% pnl={gross:+.4f} USDT")
    print("fees_slippage_excluded=true")


if __name__ == "__main__":
    main()
