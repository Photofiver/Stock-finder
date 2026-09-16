import statistics
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
TOP_N = 10
NOTIONAL = 10.0
VOL_LOOKBACK = 3
SPIKE_CAP = 2.5
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
RSI_PERIOD = 14
D1M = 60_000
D1H = 60 * D1M
WINDOW_DAYS = 7
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
            out.append({
                "ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5]),
            })
        except Exception:
            pass
    return sorted(out, key=lambda x: x["ts"])


def current_top10():
    live = set()
    for x in api_get("/api/v1/market/instruments"):
        if (x.get("state") == "live" and x.get("instType") == "SWAP"
                and x.get("contractType") == "linear" and x.get("settleCurrency") == "USDT"
                and x.get("instId")):
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
    return [{"inst": inst, "change": chg, "rank": i + 1}
            for i, (chg, inst) in enumerate(ranked[:TOP_N])]


def fetch_1h(inst):
    return parse(api_get("/api/v1/market/candles", {"instId": inst, "bar": "1H", "limit": "1440"}))


def fetch_1m_window(inst, signal_t, minutes=180):
    end_t = signal_t + minutes * D1M
    raw = api_get("/api/v1/market/candles", {
        "instId": inst, "bar": "1m", "after": str(end_t), "limit": str(minutes)
    })
    bars = parse(raw)
    return [b for b in bars if signal_t <= b["ts"] < end_t]


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
        gains.append(max(ch, 0.0)); losses.append(max(-ch, 0.0))
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


def ema(vals, period):
    out = [None] * len(vals)
    if not vals:
        return out
    a = 2.0 / (period + 1)
    out[0] = vals[0]
    for i in range(1, len(vals)):
        out[i] = a * vals[i] + (1 - a) * out[i - 1]
    return out


def macd_hist(bars):
    closes = [b["c"] for b in bars]
    fast = ema(closes, 12); slow = ema(closes, 26)
    line = [fast[i] - slow[i] for i in range(len(bars))]
    sig = ema(line, 9)
    return [line[i] - sig[i] for i in range(len(bars))]


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins = current_top10()
    rank = {c["inst"]: c["rank"] for c in coins}
    print("FROZEN_TOP10=" + ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins))

    data, idx_by_close = {}, {}
    for c in coins:
        inst = c["inst"]
        bars = fetch_1h(inst)
        k, d = stochastic(bars)
        rsi = rsi_wilder(bars, RSI_PERIOD)
        hist = macd_hist(bars)
        for i, b in enumerate(bars):
            b["k"], b["d"], b["rsi"], b["mh"] = k[i], d[i], rsi[i], hist[i]
        data[inst] = bars
        idx_by_close[inst] = {b["ts"] + D1H: i for i, b in enumerate(bars)}

    common_end = min(max(idx_by_close[inst]) for inst in data)
    common_start = common_end - WINDOW_DAYS * 24 * D1H
    min_idx = 30

    def volume_ok(bars, i):
        cur, prev = bars[i], bars[i - 1]
        if cur["v"] <= prev["v"]:
            return False
        med = statistics.median([b["v"] for b in bars[i - VOL_LOOKBACK:i]])
        return med > 0 and cur["v"] / med <= SPIKE_CAP

    def candidates(t, signal_type, rsi_filter=False):
        out = []
        for c in coins:
            inst = c["inst"]
            i = idx_by_close[inst].get(t)
            if i is None or i < min_idx:
                continue
            bars = data[inst]
            prev, cur = bars[i - 1], bars[i]
            if not volume_ok(bars, i):
                continue

            side = None
            if signal_type == "VOL":
                if cur["c"] > cur["o"]: side = "LONG"
                elif cur["c"] < cur["o"]: side = "SHORT"
            elif signal_type == "STOCH":
                if None in (prev["k"], prev["d"], cur["k"], cur["d"]):
                    continue
                if prev["k"] <= prev["d"] and cur["k"] > cur["d"]: side = "LONG"
                elif prev["k"] >= prev["d"] and cur["k"] < cur["d"]: side = "SHORT"
                if side == "LONG" and cur["c"] <= cur["o"]: continue
                if side == "SHORT" and cur["c"] >= cur["o"]: continue
            elif signal_type == "MACD":
                hp, hc = prev["mh"], cur["mh"]
                if hp <= 0 < hc: side = "LONG"
                elif hp >= 0 > hc: side = "SHORT"
                if side == "LONG" and cur["c"] <= cur["o"]: continue
                if side == "SHORT" and cur["c"] >= cur["o"]: continue
            else:
                raise ValueError(signal_type)

            if side is None:
                continue
            if rsi_filter:
                rp, rc = prev["rsi"], cur["rsi"]
                if rp is None or rc is None:
                    continue
                if side == "LONG" and not (rp <= 50 < rc): continue
                if side == "SHORT" and not (rp >= 50 > rc): continue
            out.append((rank[inst], inst, side, cur["c"]))
        out.sort(key=lambda x: x[0])
        return out

    cache_1m = {}
    def get_minutes(inst, signal_t):
        key = (inst, signal_t)
        if key not in cache_1m:
            cache_1m[key] = fetch_1m_window(inst, signal_t, 180)
        return cache_1m[key]

    def resolve(inst, side, signal_t, entry_t, entry_px, tp_pct, sl_pct, hold_h):
        bars = [b for b in get_minutes(inst, signal_t)
                if entry_t + D1M <= b["ts"] < entry_t + hold_h * D1H]
        tp = entry_px * (1 + tp_pct if side == "LONG" else 1 - tp_pct)
        sl = entry_px * (1 - sl_pct if side == "LONG" else 1 + sl_pct)
        for b in bars:
            if side == "LONG": ht, hs = b["h"] >= tp, b["l"] <= sl
            else: ht, hs = b["l"] <= tp, b["h"] >= sl
            if ht and hs:
                return "SAME_1M", b["ts"], sl
            if ht:
                return "WIN", b["ts"], tp
            if hs:
                return "LOSS", b["ts"], sl
        exit_t = entry_t + hold_h * D1H
        exit_px = bars[-1]["c"] if bars else entry_px
        return "TIME", exit_t, exit_px

    def simulate(name, signal_type="STOCH", tp=0.005, sl=0.01, hold=2,
                 rsi_filter=False, pullback=0.0):
        trades = []
        busy_until = -1
        raw_signals = 0
        skipped_pullback = 0
        t = common_start
        while t <= common_end:
            cs = candidates(t, signal_type, rsi_filter)
            if cs:
                raw_signals += 1
            if t < busy_until or not cs:
                t += D1H
                continue
            _, inst, side, sig_px = cs[0]
            entry_t, entry_px = t, sig_px
            if pullback > 0:
                threshold = sig_px * (1 - pullback if side == "LONG" else 1 + pullback)
                wait_end = t + PULLBACK_WAIT_MIN * D1M
                chosen = None
                for b in get_minutes(inst, t):
                    if not (t + D1M <= b["ts"] < wait_end + D1M):
                        continue
                    touched = b["l"] <= threshold if side == "LONG" else b["h"] >= threshold
                    if touched:
                        chosen = b
                        break
                if chosen is None:
                    skipped_pullback += 1
                    t += D1H
                    continue
                entry_t = chosen["ts"] + D1M
                entry_px = chosen["c"]
            reason, exit_t, exit_px = resolve(inst, side, t, entry_t, entry_px, tp, sl, hold)
            if reason == "SAME_1M":
                reason = "LOSS_SAME1M"
            trades.append({"side": side, "entry": entry_px, "exit": exit_px,
                           "reason": reason, "entry_t": entry_t, "exit_t": exit_t})
            busy_until = exit_t
            t += D1H

        wins = sum(x["reason"] == "WIN" for x in trades)
        losses = sum(x["reason"] in ("LOSS", "LOSS_SAME1M") for x in trades)
        same = sum(x["reason"] == "LOSS_SAME1M" for x in trades)
        times = sum(x["reason"] == "TIME" for x in trades)
        gross = 0.0
        for x in trades:
            if x["reason"] == "WIN": gross += NOTIONAL * tp
            elif x["reason"] in ("LOSS", "LOSS_SAME1M"): gross -= NOTIONAL * sl
            elif x["reason"] == "TIME":
                if x["side"] == "LONG": gross += NOTIONAL * (x["exit"] / x["entry"] - 1)
                else: gross += NOTIONAL * (1 - x["exit"] / x["entry"])
        wl = wins + losses
        return {
            "name": name, "trades": len(trades), "wins": wins, "losses": losses,
            "time": times, "same": same, "wr_all": 100 * wins / len(trades) if trades else 0,
            "wr_wl": 100 * wins / wl if wl else 0, "pnl": gross,
            "raw": raw_signals, "skipped_pb": skipped_pullback
        }

    variants = [
        ("VOL_ONLY_TP1_SL1_H1", dict(signal_type="VOL", tp=0.01, sl=0.01, hold=1)),
        ("MACD_TP1_SL1_H1", dict(signal_type="MACD", tp=0.01, sl=0.01, hold=1)),
        ("STOCH_TP1_SL1_H1", dict(tp=0.01, sl=0.01, hold=1)),
        ("STOCH_TP1_SL1_H2", dict(tp=0.01, sl=0.01, hold=2)),
        ("STOCH_TP05_SL1_H1", dict(tp=0.005, sl=0.01, hold=1)),
        ("STOCH_TP05_SL1_H2", dict(tp=0.005, sl=0.01, hold=2)),
        ("STOCH_TP05_SL05_H1", dict(tp=0.005, sl=0.005, hold=1)),
        ("STOCH_TP05_SL05_H2", dict(tp=0.005, sl=0.005, hold=2)),
        ("STOCH_RSI50_TP05_SL1_H2", dict(tp=0.005, sl=0.01, hold=2, rsi_filter=True)),
        ("STOCH_PB010_TP05_SL1_H2", dict(tp=0.005, sl=0.01, hold=2, pullback=0.001)),
        ("STOCH_PB025_TP05_SL1_H2", dict(tp=0.005, sl=0.01, hold=2, pullback=0.0025)),
        ("STOCH_PB040_TP05_SL1_H2", dict(tp=0.005, sl=0.01, hold=2, pullback=0.004)),
    ]

    print(f"COMMON_WINDOW_UTC={fmt(common_start)} -> {fmt(common_end)} days={(common_end-common_start)/86400000:.1f}")
    print("SAME_DATA=true: one frozen TOP10, one fetched 1H dataset per symbol, shared cached 1m windows across all variants")
    print("COMMON_VOLUME_RULE=current>previous and current<=2.5x median(previous3)")
    print("1m is used only for pullback execution and TP/SL ordering; fees/slippage excluded; one position at a time per variant")
    results = []
    for name, kwargs in variants:
        r = simulate(name, **kwargs)
        results.append(r)
        print(f"{name}: trades={r['trades']} wins={r['wins']} losses={r['losses']} time={r['time']} same1m={r['same']} win_all={r['wr_all']:.2f}% win_tp_sl={r['wr_wl']:.2f}% pnl={r['pnl']:+.4f} raw_signal_hours={r['raw']} skipped_pullback={r['skipped_pb']}")

    print("BY_PNL_DESC")
    for i, r in enumerate(sorted(results, key=lambda x: x["pnl"], reverse=True), 1):
        print(f"{i}. {r['name']} pnl={r['pnl']:+.4f} win_all={r['wr_all']:.2f}% trades={r['trades']}")


if __name__ == "__main__":
    main()
