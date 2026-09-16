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
VOL_MEDIAN_LOOKBACK = 3
SPIKE_CAP = 2.5
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
REQUEST_DELAY = 0.08
MAX_RETRIES = 6
D1H = 60 * 60 * 1000
START_TS = int(datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
END_TS = int(datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


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


def main():
    data = {}
    idx_by_close = {}
    body_samples = []
    for inst in SYMBOLS:
        bars = fetch_1h(inst)
        k, d = stochastic(bars)
        for i, b in enumerate(bars):
            b["k"] = k[i]
            b["d"] = d[i]
            ct = b["ts"] + D1H
            if START_TS <= ct <= END_TS and b["o"] > 0:
                body_samples.append(abs(b["c"] / b["o"] - 1) * 100)
        data[inst] = bars
        idx_by_close[inst] = {b["ts"] + D1H: i for i, b in enumerate(bars)}

    global_body_median = statistics.median(body_samples)

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
            med = statistics.median([b["v"] for b in bars[i-3:i]])
            ratio_med = cur["v"] / med if med > 0 else 999
            if ratio_med > SPIKE_CAP:
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
            body_pct = abs(cur["c"] / cur["o"] - 1) * 100
            vol_prev_ratio = cur["v"] / prev["v"] if prev["v"] > 0 else 999
            prior3_dir = 0.0
            if i >= 4 and bars[i-4]["c"] > 0:
                prior3_dir = (prev["c"] / bars[i-4]["c"] - 1) * 100
            against_prior3 = (side == "LONG" and prior3_dir < 0) or (side == "SHORT" and prior3_dir > 0)
            extreme = (side == "LONG" and cur["k"] >= 80) or (side == "SHORT" and cur["k"] <= 20)
            out.append((RANK[inst], inst, side, cur["c"], i, ratio_med, vol_prev_ratio, body_pct, prior3_dir, against_prior3, extreme, cur["k"], cur["d"]))
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
                    reason = "LOSS" if hit_sl else "WIN"
                    stop_k, stop_d = b["k"], b["d"]
                    stoch_reversed = False
                    if stop_k is not None and stop_d is not None:
                        stoch_reversed = (open_pos["side"] == "LONG" and stop_k < stop_d) or (open_pos["side"] == "SHORT" and stop_k > stop_d)
                    close_against = (open_pos["side"] == "LONG" and b["c"] < b["o"]) or (open_pos["side"] == "SHORT" and b["c"] > b["o"])
                    trades.append({**open_pos, "reason": reason, "both": bool(hit_sl and hit_tp), "exit_t": t, "hours": int((t-open_pos["entry_t"])/D1H), "stop_k": stop_k, "stop_d": stop_d, "stoch_reversed": stoch_reversed, "close_against": close_against})
                    open_pos = None
                elif t - open_pos["entry_t"] >= HOLD_HOURS * D1H:
                    trades.append({**open_pos, "reason": "TIME", "both": False, "exit_t": t, "hours": HOLD_HOURS, "stoch_reversed": False, "close_against": False})
                    open_pos = None

        if open_pos is None:
            cs = candidates(t)
            if cs:
                rank, inst, side, entry, i, ratio_med, vol_prev_ratio, body_pct, prior3_dir, against_prior3, extreme, k, d = cs[0]
                open_pos = {
                    "inst": inst, "side": side, "entry_t": t, "entry": entry,
                    "tp": entry * (1.005 if side == "LONG" else 0.995),
                    "sl": entry * (0.99 if side == "LONG" else 1.01),
                    "rank": rank, "ratio_med": ratio_med, "vol_prev_ratio": vol_prev_ratio,
                    "body_pct": body_pct, "large_body": body_pct > global_body_median,
                    "prior3_dir": prior3_dir, "against_prior3": against_prior3,
                    "extreme": extreme, "signal_k": k, "signal_d": d,
                }
        t += D1H

    losses = [x for x in trades if x["reason"] == "LOSS"]
    both = [x for x in losses if x["both"]]
    clean = [x for x in losses if not x["both"]]
    wins = [x for x in trades if x["reason"] == "WIN"]

    print("REPRO CHECK")
    print(f"trades={len(trades)} wins={len(wins)} losses={len(losses)} both_touch_losses={len(both)} clean_losses={len(clean)}")
    print(f"window={fmt(START_TS)} -> {fmt(END_TS)} frozen_symbols={','.join(SYMBOLS)}")
    print(f"global_1h_body_median={global_body_median:.3f}%")

    if not clean:
        return

    print("\nCLEAN LOSS PATTERNS")
    print(f"stop_in_1st_hour={sum(x['hours']==1 for x in clean)}/{len(clean)}")
    print(f"stop_in_2nd_hour={sum(x['hours']==2 for x in clean)}/{len(clean)}")
    print(f"exit_candle_closed_against_trade={sum(x['close_against'] for x in clean)}/{len(clean)}")
    print(f"stochastic_reversed_by_stop_candle_close={sum(x['stoch_reversed'] for x in clean)}/{len(clean)}")
    print(f"signal_in_extreme_zone_long>=80_or_short<=20={sum(x['extreme'] for x in clean)}/{len(clean)}")
    print(f"signal_against_prior_3h_direction={sum(x['against_prior3'] for x in clean)}/{len(clean)}")
    print(f"signal_body_above_market_median={sum(x['large_body'] for x in clean)}/{len(clean)}")
    print(f"signal_volume_gt_1.5x_prev={sum(x['vol_prev_ratio']>1.5 for x in clean)}/{len(clean)}")
    print(f"avg_signal_body={statistics.mean(x['body_pct'] for x in clean):.3f}%")
    print(f"avg_volume_vs_prev={statistics.mean(x['vol_prev_ratio'] for x in clean):.3f}x")
    print(f"avg_volume_vs_med3={statistics.mean(x['ratio_med'] for x in clean):.3f}x")
    print(f"long_losses={sum(x['side']=='LONG' for x in clean)} short_losses={sum(x['side']=='SHORT' for x in clean)}")

    print("\nDETAILS")
    for x in clean:
        print(
            f"{fmt(x['entry_t'])} {x['inst']} {x['side']} stop_after={x['hours']}h "
            f"body={x['body_pct']:.2f}% vol_prev={x['vol_prev_ratio']:.2f}x vol_med3={x['ratio_med']:.2f}x "
            f"K/D={x['signal_k']:.1f}/{x['signal_d']:.1f} extreme={x['extreme']} "
            f"prior3={x['prior3_dir']:+.2f}% against3h={x['against_prior3']} "
            f"exit_against={x['close_against']} stoch_reversed={x['stoch_reversed']}"
        )


if __name__ == "__main__":
    main()
