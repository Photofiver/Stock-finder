import bisect
import statistics
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
TIMEFRAMES = ["4H", "1H", "15m", "5m"]
DURATION_MS = {"4H": 14400000, "1H": 3600000, "15m": 900000, "5m": 300000}
TOP_N = 10
NOTIONAL = 10.0
TP_PCT = 0.01
SL_PCT = 0.01
MAX_HOLD_MS = 3600000
LOOKBACK = 20
SPIKE_CAPS = [None, 2.0, 3.0, 4.0]
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


def current_top10():
    live = set()
    for x in api_get("/api/v1/market/instruments"):
        if x.get("state") == "live" and x.get("instType") == "SWAP" and x.get("contractType") == "linear" and x.get("settleCurrency") == "USDT" and x.get("instId"):
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
    return [{"inst": inst, "change": change, "rank": i + 1} for i, (change, inst) in enumerate(ranked[:TOP_N])]


def fetch_recent(inst, bar):
    return parse(api_get("/api/v1/market/candles", {"instId": inst, "bar": bar, "limit": "1440"}))


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins = current_top10()
    print("CURRENT TOP10:", ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins))
    data = {c["inst"]: {tf: fetch_recent(c["inst"], tf) for tf in TIMEFRAMES} for c in coins}
    available = [c["inst"] for c in coins if all(len(data[c["inst"]][tf]) >= LOOKBACK + 2 for tf in TIMEFRAMES)]
    if not available:
        raise RuntimeError("No usable data")

    starts = {inst: data[inst]["5m"][0]["ts"] + DURATION_MS["5m"] for inst in available}
    ends = {inst: data[inst]["5m"][-1]["ts"] + DURATION_MS["5m"] for inst in available}
    start = max(starts.values())
    end = min(ends.values())
    close_times = {inst: {tf: [b["ts"] + DURATION_MS[tf] for b in data[inst][tf]] for tf in TIMEFRAMES} for inst in available}

    def sig_info(inst, tf, t_ms, cap):
        bars = data[inst][tf]
        times = close_times[inst][tf]
        i = bisect.bisect_right(times, t_ms) - 1
        if i < LOOKBACK:
            return None, None, False, None
        prev, cur = bars[i - 1], bars[i]
        close_t = cur["ts"] + DURATION_MS[tf]
        if cur["v"] <= prev["v"]:
            return None, close_t, False, None
        prior = [b["v"] for b in bars[i - LOOKBACK:i]]
        med = statistics.median(prior)
        ratio = cur["v"] / med if med > 0 else float("inf")
        is_spike = cap is not None and ratio > cap
        if is_spike:
            return None, close_t, True, ratio
        if cur["c"] > cur["o"]:
            return "LONG", close_t, False, ratio
        if cur["c"] < cur["o"]:
            return "SHORT", close_t, False, ratio
        return None, close_t, False, ratio

    def bar5(inst, t_ms):
        i = bisect.bisect_right(close_times[inst]["5m"], t_ms) - 1
        return data[inst]["5m"][i] if i >= 0 else None

    variants = []
    for cap in SPIKE_CAPS:
        variants.append({"cap": cap, "open": None, "trades": [], "entries": 0, "spike_rejects": 0,
                         "episode_active": {inst: False for inst in available}, "episode_used": {inst: False for inst in available}})

    step = DURATION_MS["5m"]
    t = start
    while t <= end:
        for v in variants:
            pos = v["open"]
            if pos:
                b = bar5(pos["inst"], t)
                if b and b["ts"] + step > pos["entry_t"]:
                    if pos["side"] == "LONG":
                        hit_sl, hit_tp = b["l"] <= pos["sl"], b["h"] >= pos["tp"]
                    else:
                        hit_sl, hit_tp = b["h"] >= pos["sl"], b["l"] <= pos["tp"]
                    if hit_sl or hit_tp:
                        loss = hit_sl
                        v["trades"].append({**pos, "reason": "LOSS" if loss else "WIN", "pnl": -0.1 if loss else 0.1})
                        v["open"] = None
                if v["open"] and t - v["open"]["entry_t"] >= MAX_HOLD_MS:
                    p = v["open"]
                    b = bar5(p["inst"], t)
                    if b:
                        d = 1 if p["side"] == "LONG" else -1
                        v["trades"].append({**p, "reason": "TIME", "pnl": NOTIONAL * (b["c"] / p["entry"] - 1) * d})
                        v["open"] = None

            candidates = []
            for c in coins:
                inst = c["inst"]
                if inst not in available:
                    continue
                infos = {tf: sig_info(inst, tf, t, v["cap"]) for tf in TIMEFRAMES}
                dirs = [infos[tf][0] for tf in TIMEFRAMES]
                side = dirs[0] if dirs[0] and all(d == dirs[0] for d in dirs) else None
                active = side is not None

                if not active:
                    v["episode_active"][inst] = False
                    v["episode_used"][inst] = False
                    if v["cap"] is not None and any(infos[tf][2] for tf in TIMEFRAMES):
                        v["spike_rejects"] += 1
                    continue
                if not v["episode_active"][inst]:
                    v["episode_active"][inst] = True
                    v["episode_used"][inst] = False
                if infos["15m"][1] != t:  # V04 fresh 15m only
                    continue
                if v["episode_used"][inst]:  # V04 one entry per continuous episode
                    continue
                b = bar5(inst, t)
                if b and b["c"] > 0:
                    candidates.append((-c["rank"], c, inst, side, b["c"]))

            if v["open"] is None and candidates:
                candidates.sort(reverse=True, key=lambda x: x[0])
                _, c, inst, side, px = candidates[0]
                v["open"] = {"inst": inst, "side": side, "entry_t": t, "entry": px,
                             "tp": px * (1.01 if side == "LONG" else 0.99),
                             "sl": px * (0.99 if side == "LONG" else 1.01)}
                v["entries"] += 1
                v["episode_used"][inst] = True
        t += step

    for v in variants:
        if v["open"]:
            p = v["open"]
            b = bar5(p["inst"], end)
            if b:
                d = 1 if p["side"] == "LONG" else -1
                v["trades"].append({**p, "reason": "END", "pnl": NOTIONAL * (b["c"] / p["entry"] - 1) * d})

    print("\nV04 SPIKE-FILTER TEST: 4/4, volume>previous, fresh15, one episode, TP=1%, SL=1%, max hold=1h")
    print(f"Spike filter: reject if current volume > cap x median(previous {LOOKBACK} bars) on ANY of 4 TFs")
    print(f"window_utc={fmt(start)} -> {fmt(end)} days={(end-start)/86400000:.1f}")
    print("cap | entries | wins | losses | time | winrate_tp_sl | gross_pnl | spike_reject_checks")
    results = []
    for v in variants:
        wins = sum(x["reason"] == "WIN" for x in v["trades"])
        losses = sum(x["reason"] == "LOSS" for x in v["trades"])
        times = sum(x["reason"] == "TIME" for x in v["trades"])
        total = sum(x["pnl"] for x in v["trades"])
        wl = wins + losses
        wr = 100 * wins / wl if wl else 0.0
        label = "NO_CAP" if v["cap"] is None else f"{v['cap']:.0f}x"
        results.append((total, wr, label, v["entries"], wins, losses, times, v["spike_rejects"]))
        print(f"{label} | {v['entries']} | {wins} | {losses} | {times} | {wr:.2f}% | {total:+.4f} USDT | {v['spike_rejects']}")
    print("\nSORTED BY GROSS PNL")
    for total, wr, label, entries, wins, losses, times, rejects in sorted(results, reverse=True):
        print(f"{label}: pnl={total:+.4f} USDT winrate={wr:.2f}% entries={entries} W/L={wins}/{losses} time={times}")
    print("NOTE: current TOP10 held fixed historically; fees/slippage excluded; TP/SL checked on 5m bars; if both touch in one bar, LOSS is assumed.")


if __name__ == "__main__":
    main()
