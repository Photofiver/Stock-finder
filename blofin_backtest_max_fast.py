import bisect
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
MAX_PAGES = 5000


def api_get(path, params=None):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(REQUEST_DELAY)
            r = requests.get(BASE + path, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(max(1.5, 1.5 * (attempt + 1)))
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
    return out


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


def fetch_all(inst, bar):
    by_ts = {}
    after = None
    pages = 0
    while pages < MAX_PAGES:
        params = {"instId": inst, "bar": bar, "limit": "1440"}
        if after is not None:
            params["after"] = str(after)
        rows = parse(api_get("/api/v1/market/candles", params))
        pages += 1
        if not rows:
            break
        before_count = len(by_ts)
        for r in rows:
            by_ts[r["ts"]] = r
        oldest = min(r["ts"] for r in rows)
        if len(by_ts) == before_count:
            break
        if after is not None and oldest >= after:
            break
        after = oldest
        if len(rows) < 1440:
            break
    return sorted(by_ts.values(), key=lambda x: x["ts"]), pages


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins = current_top10()
    print("CURRENT TOP10:", ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins))
    data = {}
    total_pages = 0

    for c in coins:
        inst = c["inst"]
        data[inst] = {}
        print(f"FETCH {inst}", flush=True)
        for tf in TIMEFRAMES:
            candles, pages = fetch_all(inst, tf)
            total_pages += pages
            data[inst][tf] = candles
            if candles:
                print(f"  {tf}: {len(candles)} bars {fmt(candles[0]['ts'])} -> {fmt(candles[-1]['ts'])} pages={pages}", flush=True)
            else:
                print(f"  {tf}: no data", flush=True)

    available = [c["inst"] for c in coins if data[c["inst"]]["5m"]]
    if not available:
        raise RuntimeError("No 5m data")

    starts = {inst: data[inst]["5m"][0]["ts"] + DURATION_MS["5m"] for inst in available}
    ends = {inst: data[inst]["5m"][-1]["ts"] + DURATION_MS["5m"] for inst in available}
    start = min(starts.values())
    end = max(ends.values())

    close_times = {
        inst: {
            tf: [b["ts"] + DURATION_MS[tf] for b in data[inst][tf]]
            for tf in TIMEFRAMES
        }
        for inst in available
    }

    def signal(inst, tf, t_ms):
        bars = data[inst][tf]
        times = close_times[inst][tf]
        i = bisect.bisect_right(times, t_ms) - 1
        if i < 1:
            return None
        prev, cur = bars[i - 1], bars[i]
        if cur["v"] <= prev["v"]:
            return None
        if cur["c"] > cur["o"]:
            return "LONG"
        if cur["c"] < cur["o"]:
            return "SHORT"
        return None

    def closed_5m_bar(inst, t_ms):
        times = close_times[inst]["5m"]
        i = bisect.bisect_right(times, t_ms) - 1
        if i < 0:
            return None
        return data[inst]["5m"][i]

    trades = []
    entries = 0
    open_pos = None
    t = start
    step = DURATION_MS["5m"]

    while t <= end:
        if open_pos:
            inst = open_pos["inst"]
            b = closed_5m_bar(inst, t)
            if b and b["ts"] + step > open_pos["entry_t"]:
                if open_pos["side"] == "LONG":
                    hit_sl = b["l"] <= open_pos["sl"]
                    hit_tp = b["h"] >= open_pos["tp"]
                else:
                    hit_sl = b["h"] >= open_pos["sl"]
                    hit_tp = b["l"] <= open_pos["tp"]
                if hit_sl or hit_tp:
                    # Conservative: if both occur in one 5m bar, LOSS.
                    loss = hit_sl
                    trades.append({
                        **open_pos,
                        "exit_t": t,
                        "exit": open_pos["sl"] if loss else open_pos["tp"],
                        "reason": "LOSS" if loss else "WIN",
                        "pnl": -NOTIONAL * SL_PCT if loss else NOTIONAL * TP_PCT,
                    })
                    open_pos = None
            if open_pos and t - open_pos["entry_t"] >= MAX_HOLD_MS:
                b = closed_5m_bar(open_pos["inst"], t)
                if b:
                    px = b["c"]
                    d = 1 if open_pos["side"] == "LONG" else -1
                    pnl = NOTIONAL * (px / open_pos["entry"] - 1) * d
                    trades.append({**open_pos, "exit_t": t, "exit": px, "reason": "TIME", "pnl": pnl})
                    open_pos = None

        if open_pos is None:
            candidates = []
            for c in coins:
                inst = c["inst"]
                if inst not in available or not (starts[inst] <= t <= ends[inst]):
                    continue
                sigs = {tf: signal(inst, tf, t) for tf in TIMEFRAMES}
                longs = sum(v == "LONG" for v in sigs.values())
                shorts = sum(v == "SHORT" for v in sigs.values())
                if longs >= 3 or shorts >= 3:
                    side = "LONG" if longs > shorts else "SHORT"
                    matches = max(longs, shorts)
                    b = closed_5m_bar(inst, t)
                    if b and b["c"] > 0:
                        candidates.append((matches, -c["rank"], c, side, sigs, b["c"]))
            if candidates:
                candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
                matches, _, c, side, sigs, px = candidates[0]
                open_pos = {
                    "inst": c["inst"],
                    "rank": c["rank"],
                    "side": side,
                    "matches": matches,
                    "sigs": sigs,
                    "entry_t": t,
                    "entry": px,
                    "tp": px * (1 + TP_PCT) if side == "LONG" else px * (1 - TP_PCT),
                    "sl": px * (1 - SL_PCT) if side == "LONG" else px * (1 + SL_PCT),
                }
                entries += 1
        t += step

    if open_pos:
        b = closed_5m_bar(open_pos["inst"], end)
        if b:
            px = b["c"]
            d = 1 if open_pos["side"] == "LONG" else -1
            pnl = NOTIONAL * (px / open_pos["entry"] - 1) * d
            trades.append({**open_pos, "exit_t": end, "exit": px, "reason": "END", "pnl": pnl})

    wins = sum(x["reason"] == "WIN" for x in trades)
    losses = sum(x["reason"] == "LOSS" for x in trades)
    times = sum(x["reason"] == "TIME" for x in trades)
    ends_n = sum(x["reason"] == "END" for x in trades)
    total = sum(x["pnl"] for x in trades)
    wl = wins + losses
    wr = wins / wl * 100 if wl else 0.0

    print("\n===== FAST MAX-HISTORY SUMMARY =====")
    print(f"window_utc={fmt(start)} -> {fmt(end)}")
    print(f"days={(end-start)/86400000:.1f}")
    print(f"entries={entries} closed={len(trades)}")
    print(f"wins={wins} losses={losses} time_exits={times} end_exits={ends_n}")
    print(f"tp_sl_win_rate={wr:.2f}% ({wins}/{wl})")
    print(f"gross_pnl={total:+.4f} USDT on max 10 USDT notional")
    print(f"api_pages={total_pages}")
    print("EARLIEST 5m BY COIN:")
    for c in coins:
        inst = c["inst"]
        if inst in starts:
            print(f"  #{c['rank']} {inst}: {fmt(starts[inst])}")

    by_coin = {}
    for tr in trades:
        s = by_coin.setdefault(tr["inst"], {"n": 0, "w": 0, "l": 0, "pnl": 0.0})
        s["n"] += 1
        s["w"] += tr["reason"] == "WIN"
        s["l"] += tr["reason"] == "LOSS"
        s["pnl"] += tr["pnl"]
    print("BY COIN:")
    for c in coins:
        s = by_coin.get(c["inst"])
        if s:
            print(f"  {c['inst']}: trades={s['n']} wins={s['w']} losses={s['l']} pnl={s['pnl']:+.4f}")

    print("NOTE: Current BloFin TOP10 is held fixed across historical time because the public API has no historical TOP10 ranking endpoint. Entry signals are exact on closed 4H/1H/15m/5m candles. TP/SL is evaluated on 5m bars; if both levels are touched in one bar, LOSS is assumed. Fees/slippage excluded.")


if __name__ == "__main__":
    main()
