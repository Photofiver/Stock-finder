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
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
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


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period
    return out


def stochastic(bars):
    raw_k = [None] * len(bars)
    for i in range(STOCH_K_PERIOD - 1, len(bars)):
        window = bars[i - STOCH_K_PERIOD + 1:i + 1]
        hh = max(b["h"] for b in window)
        ll = min(b["l"] for b in window)
        raw_k[i] = 50.0 if hh == ll else 100.0 * (bars[i]["c"] - ll) / (hh - ll)
    k = sma(raw_k, STOCH_K_SMOOTH)
    d = sma(k, STOCH_D_PERIOD)
    return k, d


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins = current_top10()
    print("CURRENT TOP10:", ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins))

    data = {}
    close_times = {}
    index_by_close = {}
    for c in coins:
        inst = c["inst"]
        bars = fetch_1h(inst)
        k, d = stochastic(bars)
        for i in range(len(bars)):
            bars[i]["stoch_k"] = k[i]
            bars[i]["stoch_d"] = d[i]
        data[inst] = bars
        close_times[inst] = [b["ts"] + D1H for b in bars]
        index_by_close[inst] = {b["ts"] + D1H: i for i, b in enumerate(bars)}

    min_idx = STOCH_K_PERIOD + STOCH_K_SMOOTH + STOCH_D_PERIOD + 2
    available = [c["inst"] for c in coins if len(data[c["inst"]]) >= min_idx + 3]
    if not available:
        raise RuntimeError("No usable 1H data")

    start = max(close_times[inst][min_idx] for inst in available)
    end = min(close_times[inst][-1] for inst in available)

    def signal_candidates(t):
        candidates = []
        for c in coins:
            inst = c["inst"]
            if inst not in available:
                continue
            i = index_by_close[inst].get(t)
            if i is None or i < min_idx:
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

            pk, pd = prev["stoch_k"], prev["stoch_d"]
            ck, cd = cur["stoch_k"], cur["stoch_d"]
            if None in (pk, pd, ck, cd):
                continue

            if pk <= pd and ck > cd:
                side = "LONG"
            elif pk >= pd and ck < cd:
                side = "SHORT"
            else:
                continue

            if side == "LONG" and not (cur["c"] > cur["o"]):
                continue
            if side == "SHORT" and not (cur["c"] < cur["o"]):
                continue

            candidates.append((-c["rank"], c, inst, side, cur["c"], ratio, ck, cd))

        candidates.sort(reverse=True, key=lambda x: x[0])
        return candidates

    def simulate(max_hold_hours):
        open_pos = None
        trades = []
        entries = 0
        t = start

        while t <= end:
            # First process the just-closed 1H candle for an existing position.
            if open_pos is not None and t > open_pos["entry_close_t"]:
                inst = open_pos["inst"]
                i = index_by_close[inst].get(t)
                if i is not None:
                    b = data[inst][i]
                    if open_pos["side"] == "LONG":
                        hit_sl = b["l"] <= open_pos["sl"]
                        hit_tp = b["h"] >= open_pos["tp"]
                    else:
                        hit_sl = b["h"] >= open_pos["sl"]
                        hit_tp = b["l"] <= open_pos["tp"]

                    if hit_sl or hit_tp:
                        loss = hit_sl  # conservative when both touch in same 1H candle
                        trades.append({**open_pos, "exit_t": t, "reason": "LOSS" if loss else "WIN", "pnl": -NOTIONAL * SL_PCT if loss else NOTIONAL * TP_PCT})
                        open_pos = None
                    elif t - open_pos["entry_close_t"] >= max_hold_hours * D1H:
                        dsign = 1 if open_pos["side"] == "LONG" else -1
                        pnl = NOTIONAL * (b["c"] / open_pos["entry"] - 1) * dsign
                        trades.append({**open_pos, "exit_t": t, "reason": "TIME", "pnl": pnl})
                        open_pos = None

            # At xx:01, after processing expiry/exit, a new signal may be taken if flat.
            if open_pos is None:
                candidates = signal_candidates(t)
                if candidates:
                    _, c, inst, side, entry, ratio, ck, cd = candidates[0]
                    open_pos = {
                        "inst": inst,
                        "side": side,
                        "entry_close_t": t,
                        "check_t": t + 60_000,
                        "entry": entry,
                        "ratio": ratio,
                        "stoch_k": ck,
                        "stoch_d": cd,
                        "tp": entry * (1.01 if side == "LONG" else 0.99),
                        "sl": entry * (0.99 if side == "LONG" else 1.01),
                    }
                    entries += 1

            t += D1H

        if open_pos is not None:
            inst = open_pos["inst"]
            i = index_by_close[inst].get(end)
            if i is not None:
                b = data[inst][i]
                dsign = 1 if open_pos["side"] == "LONG" else -1
                pnl = NOTIONAL * (b["c"] / open_pos["entry"] - 1) * dsign
                trades.append({**open_pos, "exit_t": end, "reason": "END", "pnl": pnl})

        wins = sum(x["reason"] == "WIN" for x in trades)
        losses = sum(x["reason"] == "LOSS" for x in trades)
        times = sum(x["reason"] == "TIME" for x in trades)
        ends = sum(x["reason"] == "END" for x in trades)
        total = sum(x["pnl"] for x in trades)
        wl = wins + losses
        wr = 100 * wins / wl if wl else 0.0
        return {
            "hold": max_hold_hours,
            "entries": entries,
            "closed": len(trades),
            "wins": wins,
            "losses": losses,
            "time": times,
            "end": ends,
            "wr": wr,
            "wl": wl,
            "pnl": total,
        }

    r1 = simulate(1)
    r2 = simulate(2)

    print("\n1H STOCHASTIC HOLD-TIME COMPARISON")
    print("Same frozen TOP10 and same fetched 1H candles for both variants.")
    print("Rules: ONLY 1H; check at xx:01; volume>previous; volume<=2.5x median(previous 3); Stochastic 14,3,3 K/D cross; candle color agrees; TP=1%; SL=1%; one position at a time")
    print(f"window_utc={fmt(start)} -> {fmt(end)} days={(end-start)/86400000:.1f}")
    for r in (r1, r2):
        print(f"HOLD_{r['hold']}H entries={r['entries']} closed={r['closed']} wins={r['wins']} losses={r['losses']} time_exits={r['time']} end_exits={r['end']} tp_sl_win_rate={r['wr']:.2f}% ({r['wins']}/{r['wl']}) gross_pnl={r['pnl']:+.4f} USDT")
    print(f"DELTA_2H_MINUS_1H={r2['pnl'] - r1['pnl']:+.4f} USDT")
    print("NOTE: current TOP10 held fixed historically; fees/slippage excluded; if TP and SL both touch in the same 1H candle, LOSS is assumed; entry uses the just-closed 1H close as xx:01 price proxy.")


if __name__ == "__main__":
    main()
