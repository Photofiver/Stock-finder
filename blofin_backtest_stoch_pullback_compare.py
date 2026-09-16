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
D1M = 60_000
D1H = 60 * D1M
START_TS = int(datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
END_TS = int(datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
REQUEST_DELAY = 0.08
MAX_RETRIES = 6
PULLBACK_WAIT_MIN = 30


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


def fetch_1m(inst, end_ts, limit=180):
    return parse(api_get("/api/v1/market/candles", {"instId": inst, "bar": "1m", "after": str(end_ts), "limit": str(limit)}))


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

    signals = []
    t = START_TS
    while t <= END_TS:
        cs = candidates(t)
        if cs:
            _, inst, side, signal_price = cs[0]
            signals.append({"t": t, "inst": inst, "side": side, "signal_price": signal_price})
        t += D1H

    cache_1m = {}
    def minute_window(inst, start_t, end_t):
        key = (inst, end_t)
        if key not in cache_1m:
            cache_1m[key] = fetch_1m(inst, end_t, 180)
        return [b for b in cache_1m[key] if start_t <= b["ts"] < end_t]

    def resolve_after_entry(inst, side, entry_t, entry_price):
        end_t = entry_t + HOLD_HOURS * D1H
        bars = minute_window(inst, entry_t + D1M, end_t)
        tp = entry_price * (1 + TP_PCT if side == "LONG" else 1 - TP_PCT)
        sl = entry_price * (1 - SL_PCT if side == "LONG" else 1 + SL_PCT)
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
        exit_price = bars[-1]["c"] if bars else entry_price
        return "TIME", end_t, exit_price

    def run_variant(pb_pct):
        trades = []
        skipped_no_pullback = 0
        blocked_signals = 0
        busy_until = -1
        for s in signals:
            if s["t"] < busy_until:
                blocked_signals += 1
                continue
            inst, side, sig_t, sig_px = s["inst"], s["side"], s["t"], s["signal_price"]
            if pb_pct == 0:
                entry_t = sig_t
                entry_px = sig_px
            else:
                wait_end = sig_t + PULLBACK_WAIT_MIN * D1M
                bars = minute_window(inst, sig_t + D1M, wait_end + D1M)
                threshold = sig_px * (1 - pb_pct if side == "LONG" else 1 + pb_pct)
                chosen = None
                for b in bars:
                    touched = b["l"] <= threshold if side == "LONG" else b["h"] >= threshold
                    if touched:
                        chosen = b
                        break
                if chosen is None:
                    skipped_no_pullback += 1
                    continue
                entry_t = chosen["ts"] + D1M
                entry_px = chosen["c"]
            reason, exit_t, exit_px = resolve_after_entry(inst, side, entry_t, entry_px)
            trades.append({"reason": reason, "entry_t": entry_t, "entry": entry_px, "exit_t": exit_t, "exit": exit_px, "side": side})
            busy_until = exit_t

        wins = sum(x["reason"] == "WIN" for x in trades)
        losses = sum(x["reason"] == "LOSS" for x in trades)
        times = sum(x["reason"] == "TIME" for x in trades)
        same = sum(x["reason"] == "SAME_1M" for x in trades)
        gross = 0.0
        for x in trades:
            if x["reason"] == "WIN": gross += NOTIONAL * TP_PCT
            elif x["reason"] == "LOSS": gross -= NOTIONAL * SL_PCT
            elif x["reason"] == "TIME":
                if x["side"] == "LONG": gross += NOTIONAL * (x["exit"] / x["entry"] - 1)
                else: gross += NOTIONAL * (1 - x["exit"] / x["entry"])
        resolved = wins + losses
        return {
            "pb": pb_pct, "trades": len(trades), "wins": wins, "losses": losses, "time": times, "same": same,
            "wr_all": 100*wins/len(trades) if trades else 0, "wr_res": 100*wins/resolved if resolved else 0,
            "pnl": gross, "skipped": skipped_no_pullback, "blocked": blocked_signals
        }

    print(f"window={fmt(START_TS)} -> {fmt(END_TS)} signals={len(signals)}")
    print("Rules: 1H Stochastic 14,3,3 cross + candle color + volume>previous + volume<=2.5x median(prev3), TP=0.5%, SL=1.0%, max hold=2h, one position at a time.")
    print("Pullback variants: after 1H signal at xx:01, wait max 30m for adverse retrace; enter at close of first 1m candle that touches threshold. 1m is only for execution/exit ordering.")
    for pb in (0.0, 0.001, 0.0025, 0.004):
        r = run_variant(pb)
        label = "BASE_IMMEDIATE" if pb == 0 else f"PULLBACK_{pb*100:.2f}%"
        print(f"{label}: trades={r['trades']} wins={r['wins']} losses={r['losses']} time={r['time']} same1m={r['same']} win_all={r['wr_all']:.2f}% win_tp_sl={r['wr_res']:.2f}% pnl={r['pnl']:+.4f} skipped_no_pullback={r['skipped']} blocked={r['blocked']}")
    print("fees_slippage_excluded=true")


if __name__ == "__main__":
    main()
