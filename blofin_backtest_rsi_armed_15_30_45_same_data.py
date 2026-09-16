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
D5M = 5 * D1M
REQUEST_DELAY = 0.08
MAX_RETRIES = 6
INTERVALS = [15, 30, 45]


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


def fetch_paginated(inst, bar, step_ms, start_needed, end_needed, max_pages=20):
    all_rows = {}
    cursor = end_needed + step_ms
    for _ in range(max_pages):
        raw = api_get("/api/v1/market/candles", {
            "instId": inst, "bar": bar, "after": str(cursor), "limit": "1440"
        })
        bars = parse(raw)
        if not bars:
            break
        for b in bars:
            all_rows[b["ts"]] = b
        oldest = min(b["ts"] for b in bars)
        if oldest <= start_needed:
            break
        if oldest >= cursor:
            break
        cursor = oldest
    return sorted(
        [b for b in all_rows.values() if start_needed <= b["ts"] < end_needed],
        key=lambda x: x["ts"],
    )


def aggregate_5m(bars, minutes):
    interval_ms = minutes * D1M
    need = minutes // 5
    buckets = {}
    for b in bars:
        bucket = (b["ts"] // interval_ms) * interval_ms
        buckets.setdefault(bucket, []).append(b)
    out = []
    for ts in sorted(buckets):
        group = sorted(buckets[ts], key=lambda x: x["ts"])
        if len(group) != need:
            continue
        if group[0]["ts"] != ts:
            continue
        if any(group[j]["ts"] != ts + j * D5M for j in range(need)):
            continue
        out.append({
            "ts": ts,
            "o": group[0]["o"],
            "h": max(x["h"] for x in group),
            "l": min(x["l"] for x in group),
            "c": group[-1]["c"],
            "v": sum(x["v"] for x in group),
        })
    return out


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


def simulate(minutes, source_5m, source_1m):
    interval_ms = minutes * D1M
    data = {}
    by_close = {}
    all_closes = set()

    for inst in SYMBOLS:
        bars = aggregate_5m(source_5m[inst], minutes)
        k, d = stochastic(bars)
        rsi = rsi_wilder(bars, RSI_PERIOD)
        for i, b in enumerate(bars):
            b["k"], b["d"], b["rsi"] = k[i], d[i], rsi[i]
        data[inst] = bars
        by_close[inst] = {b["ts"] + interval_ms: i for i, b in enumerate(bars)}
        all_closes.update(t for t in by_close[inst] if START_TS <= t <= END_TS)

    armed = {s: None for s in SYMBOLS}
    used = {s: False for s in SYMBOLS}
    busy_until = -1
    trades = []
    raw_candidates = 0
    blocked_same_coin = 0
    rsi_crosses = 0
    min_idx = 30

    for t in sorted(all_closes):
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
            continue

        candidates.sort(key=lambda x: x[0])
        _, inst, side, entry_px = candidates[0]
        entry_t = t
        used[inst] = True

        bars1m = [b for b in source_1m[inst]
                  if entry_t + D1M <= b["ts"] < entry_t + HOLD_MIN * D1M]
        tp = entry_px * (1 + TP_PCT if side == "LONG" else 1 - TP_PCT)
        sl = entry_px * (1 - SL_PCT if side == "LONG" else 1 + SL_PCT)
        reason = "TIME"
        exit_t = entry_t + HOLD_MIN * D1M
        exit_px = bars1m[-1]["c"] if bars1m else entry_px
        same1m = False

        for b in bars1m:
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
    return {
        "minutes": minutes,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "time": times,
        "same": same,
        "win_all": 100 * wins / len(trades) if trades else 0.0,
        "win_tp_sl": 100 * wins / wl if wl else 0.0,
        "pnl": gross,
        "rsi_crosses": rsi_crosses,
        "raw_candidates": raw_candidates,
        "blocked": blocked_same_coin,
    }


def main():
    # Use one identical frozen 5m source for all three signal intervals.
    warmup_start = START_TS - 3 * 24 * 60 * D1M
    exit_end = END_TS + HOLD_MIN * D1M
    source_5m = {}
    source_1m = {}

    for inst in SYMBOLS:
        source_5m[inst] = fetch_paginated(inst, "5m", D5M, warmup_start, END_TS, max_pages=4)
        source_1m[inst] = fetch_paginated(inst, "1m", D1M, START_TS, exit_end, max_pages=10)
        print(f"FETCHED {inst} 5m={len(source_5m[inst])} 1m={len(source_1m[inst])}")

    print("FROZEN_DATA=true")
    print("SYMBOLS=" + ",".join(SYMBOLS))
    print(f"WINDOW={fmt(START_TS)} -> {fmt(END_TS)} UTC")
    print("COMMON=RSI14 cross 50 arms coin; wait for same-direction Stochastic 14,3,3 cross; candle color agrees; volume>previous and <=2.5x median(prev3); one entry per coin until next RSI cross; one global position; TP0.5%; SL1%; max2h; 1m exit ordering")
    print("SIGNAL_BARS=15m,30m,45m all aggregated from the same frozen 5m candles")

    results = []
    for minutes in INTERVALS:
        r = simulate(minutes, source_5m, source_1m)
        results.append(r)
        print(
            f"RESULT_{minutes}M: trades={r['trades']} wins={r['wins']} losses={r['losses']} "
            f"time={r['time']} same1m={r['same']} win_all={r['win_all']:.2f}% "
            f"win_tp_sl={r['win_tp_sl']:.2f}% pnl={r['pnl']:+.4f} USDT "
            f"rsi_crosses={r['rsi_crosses']} raw_candidate_times={r['raw_candidates']} "
            f"blocked_same_coin_checks={r['blocked']}"
        )

    print("BY_PNL_DESC")
    for i, r in enumerate(sorted(results, key=lambda x: x["pnl"], reverse=True), 1):
        print(f"{i}. {r['minutes']}m pnl={r['pnl']:+.4f} win_tp_sl={r['win_tp_sl']:.2f}% win_all={r['win_all']:.2f}% trades={r['trades']}")
    print("fees_slippage_excluded=true")


if __name__ == "__main__":
    main()
