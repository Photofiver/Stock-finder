import bisect
import itertools
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
TIMEFRAMES = ["4H", "1H", "15m", "5m"]
DURATION_MS = {
    "4H": 4 * 60 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "5m": 5 * 60 * 1000,
}
TOP_N = 10
NOTIONAL = 10.0
TP_PCT = 0.01
SL_PCT = 0.01
MAX_HOLD_MS = 60 * 60 * 1000
REQUEST_DELAY = 0.08
MAX_RETRIES = 6
VOL_MULTS = [1.0, 1.2, 1.5]
FRESH_MODES = ["STATE", "FRESH15"]  # FRESH15: the 15m candle must have just closed at this scan time
ENTRY_MODES = ["REPEAT", "ONE_EPISODE"]  # ONE_EPISODE: max one entry during one continuous 4/4 episode


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
        {"inst": inst, "change": change, "rank": idx + 1}
        for idx, (change, inst) in enumerate(ranked[:TOP_N])
    ]


def fetch_recent(inst, bar):
    return parse(api_get("/api/v1/market/candles", {"instId": inst, "bar": bar, "limit": "1440"}))


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins = current_top10()
    print("CURRENT TOP10:", ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins))

    data = {}
    for c in coins:
        inst = c["inst"]
        data[inst] = {}
        for tf in TIMEFRAMES:
            data[inst][tf] = fetch_recent(inst, tf)

    available = [c["inst"] for c in coins if all(len(data[c["inst"]][tf]) >= 2 for tf in TIMEFRAMES)]
    if not available:
        raise RuntimeError("No usable data")

    starts = {inst: data[inst]["5m"][0]["ts"] + DURATION_MS["5m"] for inst in available}
    ends = {inst: data[inst]["5m"][-1]["ts"] + DURATION_MS["5m"] for inst in available}
    start = max(starts.values())
    end = min(ends.values())

    close_times = {
        inst: {
            tf: [b["ts"] + DURATION_MS[tf] for b in data[inst][tf]]
            for tf in TIMEFRAMES
        }
        for inst in available
    }

    def sig_info(inst, tf, t_ms, vol_mult):
        bars = data[inst][tf]
        times = close_times[inst][tf]
        i = bisect.bisect_right(times, t_ms) - 1
        if i < 1:
            return None, None
        prev, cur = bars[i - 1], bars[i]
        if cur["v"] < prev["v"] * vol_mult:
            return None, cur["ts"] + DURATION_MS[tf]
        if cur["c"] > cur["o"]:
            return "LONG", cur["ts"] + DURATION_MS[tf]
        if cur["c"] < cur["o"]:
            return "SHORT", cur["ts"] + DURATION_MS[tf]
        return None, cur["ts"] + DURATION_MS[tf]

    def bar5(inst, t_ms):
        times = close_times[inst]["5m"]
        i = bisect.bisect_right(times, t_ms) - 1
        return data[inst]["5m"][i] if i >= 0 else None

    variants = []
    for vol_mult, fresh_mode, entry_mode in itertools.product(VOL_MULTS, FRESH_MODES, ENTRY_MODES):
        variants.append({
            "vol": vol_mult,
            "fresh": fresh_mode,
            "entry_mode": entry_mode,
            "open": None,
            "trades": [],
            "entries": 0,
            "episode_active": {inst: False for inst in available},
            "episode_used": {inst: False for inst in available},
        })

    step = DURATION_MS["5m"]
    t = start
    while t <= end:
        for v in variants:
            pos = v["open"]
            if pos:
                b = bar5(pos["inst"], t)
                if b and b["ts"] + step > pos["entry_t"]:
                    if pos["side"] == "LONG":
                        hit_sl = b["l"] <= pos["sl"]
                        hit_tp = b["h"] >= pos["tp"]
                    else:
                        hit_sl = b["h"] >= pos["sl"]
                        hit_tp = b["l"] <= pos["tp"]
                    if hit_sl or hit_tp:
                        loss = hit_sl  # conservative if both touch in same 5m bar
                        v["trades"].append({
                            **pos,
                            "reason": "LOSS" if loss else "WIN",
                            "pnl": -NOTIONAL * SL_PCT if loss else NOTIONAL * TP_PCT,
                        })
                        v["open"] = None
                if v["open"] and t - v["open"]["entry_t"] >= MAX_HOLD_MS:
                    p = v["open"]
                    b = bar5(p["inst"], t)
                    if b:
                        d = 1 if p["side"] == "LONG" else -1
                        pnl = NOTIONAL * (b["c"] / p["entry"] - 1) * d
                        v["trades"].append({**p, "reason": "TIME", "pnl": pnl})
                        v["open"] = None

            candidates = []
            for c in coins:
                inst = c["inst"]
                if inst not in available:
                    continue
                infos = {tf: sig_info(inst, tf, t, v["vol"]) for tf in TIMEFRAMES}
                dirs = [infos[tf][0] for tf in TIMEFRAMES]
                side = dirs[0] if dirs and dirs[0] and all(d == dirs[0] for d in dirs) else None
                active = side is not None

                if not active:
                    v["episode_active"][inst] = False
                    v["episode_used"][inst] = False
                    continue

                if not v["episode_active"][inst]:
                    v["episode_active"][inst] = True
                    v["episode_used"][inst] = False

                if v["fresh"] == "FRESH15" and infos["15m"][1] != t:
                    continue
                if v["entry_mode"] == "ONE_EPISODE" and v["episode_used"][inst]:
                    continue

                b = bar5(inst, t)
                if b and b["c"] > 0:
                    candidates.append((-c["rank"], c, inst, side, b["c"]))

            if v["open"] is None and candidates:
                candidates.sort(reverse=True, key=lambda x: x[0])
                _, c, inst, side, px = candidates[0]
                v["open"] = {
                    "inst": inst,
                    "side": side,
                    "entry_t": t,
                    "entry": px,
                    "tp": px * (1 + TP_PCT) if side == "LONG" else px * (1 - TP_PCT),
                    "sl": px * (1 - SL_PCT) if side == "LONG" else px * (1 + SL_PCT),
                }
                v["entries"] += 1
                if v["entry_mode"] == "ONE_EPISODE":
                    v["episode_used"][inst] = True
        t += step

    for v in variants:
        if v["open"]:
            p = v["open"]
            b = bar5(p["inst"], end)
            if b:
                d = 1 if p["side"] == "LONG" else -1
                pnl = NOTIONAL * (b["c"] / p["entry"] - 1) * d
                v["trades"].append({**p, "reason": "END", "pnl": pnl})
            v["open"] = None

    print("\n12 VARIANTS: all use 4/4 same-direction volume bars, TP=1%, SL=1%, max hold=1h")
    print(f"window_utc={fmt(start)} -> {fmt(end)} days={(end-start)/86400000:.1f}")
    print("variant | vol | freshness | entries | wins | losses | time | winrate_tp_sl | gross_pnl")

    results = []
    for idx, v in enumerate(variants, start=1):
        wins = sum(x["reason"] == "WIN" for x in v["trades"])
        losses = sum(x["reason"] == "LOSS" for x in v["trades"])
        times = sum(x["reason"] == "TIME" for x in v["trades"])
        total = sum(x["pnl"] for x in v["trades"])
        wl = wins + losses
        wr = wins / wl * 100 if wl else 0.0
        name = f"V{idx:02d}"
        results.append((total, wr, name, v, wins, losses, times))
        print(f"{name} | {v['vol']:.1f}x | {v['fresh']}/{v['entry_mode']} | {v['entries']} | {wins} | {losses} | {times} | {wr:.2f}% | {total:+.4f} USDT")

    print("\nSORTED BY GROSS PNL")
    for total, wr, name, v, wins, losses, times in sorted(results, reverse=True, key=lambda x: x[0]):
        print(f"{name}: pnl={total:+.4f} USDT winrate={wr:.2f}% entries={v['entries']} vol={v['vol']:.1f}x {v['fresh']}/{v['entry_mode']}")

    print("NOTE: current TOP10 is held fixed historically; fees/slippage excluded; TP/SL checked on 5m bars; if TP and SL both touch in one 5m bar, LOSS is assumed.")


if __name__ == "__main__":
    main()
