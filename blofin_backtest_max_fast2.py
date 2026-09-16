import bisect
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
TIMEFRAMES = ["4H", "1H", "15m", "5m"]
DUR = {"4H": 14400000, "1H": 3600000, "15m": 900000, "5m": 300000}
TOP_N = 10
NOTIONAL = 10.0
TP = 0.01
SL = 0.01
MAX_HOLD = 3600000
DELAY = 0.08
RETRIES = 6
MAX_PAGES = 5000


def get(path, params=None):
    last = None
    for attempt in range(RETRIES):
        try:
            time.sleep(DELAY)
            r = requests.get(BASE + path, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(max(1.5, 1.5 * (attempt + 1)))
                continue
            r.raise_for_status()
            p = r.json()
            if str(p.get("code")) != "0":
                raise RuntimeError(p)
            d = p.get("data", [])
            if not isinstance(d, list):
                raise RuntimeError("non-list")
            return d
        except Exception as exc:
            last = exc
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


def parse(raw):
    out = []
    for row in raw:
        if len(row) < 9 or str(row[8]) != "1":
            continue
        try:
            out.append({"ts": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4]), "v": float(row[5])})
        except Exception:
            pass
    return out


def top10():
    live = set()
    for x in get("/api/v1/market/instruments"):
        if x.get("state") == "live" and x.get("instType") == "SWAP" and x.get("contractType") == "linear" and x.get("settleCurrency") == "USDT" and x.get("instId"):
            live.add(x["instId"])
    ranked = []
    for x in get("/api/v1/market/tickers"):
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
    return [{"inst": inst, "change": ch, "rank": i + 1} for i, (ch, inst) in enumerate(ranked[:TOP_N])]


def fetch_all(inst, bar):
    by_ts = {}
    cursor = None
    pages = 0
    last_oldest = None
    while pages < MAX_PAGES:
        params = {"instId": inst, "bar": bar, "limit": "1440"}
        if cursor is not None:
            params["after"] = str(cursor)
        raw = get("/api/v1/market/candles", params)
        rows = parse(raw)
        pages += 1
        if not rows:
            break
        for row in rows:
            by_ts[row["ts"]] = row
        oldest = min(row["ts"] for row in rows)
        if last_oldest is not None and oldest >= last_oldest:
            break
        last_oldest = oldest
        cursor = oldest
        if pages % 25 == 0:
            print(f"    {bar}: pages={pages} bars={len(by_ts)} oldest={fmt(oldest)}", flush=True)
    return sorted(by_ts.values(), key=lambda x: x["ts"]), pages


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins = top10()
    print("CURRENT TOP10:", ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins), flush=True)
    data = {}
    total_pages = 0
    for c in coins:
        inst = c["inst"]
        data[inst] = {}
        print(f"FETCH {inst}", flush=True)
        for tf in TIMEFRAMES:
            bars, pages = fetch_all(inst, tf)
            total_pages += pages
            data[inst][tf] = bars
            if bars:
                print(f"  {tf}: {len(bars)} bars {fmt(bars[0]['ts'])} -> {fmt(bars[-1]['ts'])} pages={pages}", flush=True)
            else:
                print(f"  {tf}: no data", flush=True)

    available = [c["inst"] for c in coins if data[c["inst"]]["5m"]]
    starts = {inst: data[inst]["5m"][0]["ts"] + DUR["5m"] for inst in available}
    ends = {inst: data[inst]["5m"][-1]["ts"] + DUR["5m"] for inst in available}
    start = min(starts.values())
    end = max(ends.values())
    close_times = {inst: {tf: [b["ts"] + DUR[tf] for b in data[inst][tf]] for tf in TIMEFRAMES} for inst in available}

    def sig(inst, tf, t):
        bars = data[inst][tf]
        times = close_times[inst][tf]
        i = bisect.bisect_right(times, t) - 1
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

    def bar5(inst, t):
        times = close_times[inst]["5m"]
        i = bisect.bisect_right(times, t) - 1
        return data[inst]["5m"][i] if i >= 0 else None

    trades = []
    entries = 0
    pos = None
    t = start
    step = DUR["5m"]
    while t <= end:
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
                    loss = hit_sl
                    trades.append({**pos, "exit_t": t, "reason": "LOSS" if loss else "WIN", "pnl": -NOTIONAL * SL if loss else NOTIONAL * TP})
                    pos = None
            if pos and t - pos["entry_t"] >= MAX_HOLD:
                b = bar5(pos["inst"], t)
                if b:
                    d = 1 if pos["side"] == "LONG" else -1
                    pnl = NOTIONAL * (b["c"] / pos["entry"] - 1) * d
                    trades.append({**pos, "exit_t": t, "reason": "TIME", "pnl": pnl})
                    pos = None

        if pos is None:
            candidates = []
            for c in coins:
                inst = c["inst"]
                if inst not in available or not (starts[inst] <= t <= ends[inst]):
                    continue
                sigs = {tf: sig(inst, tf, t) for tf in TIMEFRAMES}
                lc = sum(v == "LONG" for v in sigs.values())
                sc = sum(v == "SHORT" for v in sigs.values())
                if lc >= 3 or sc >= 3:
                    side = "LONG" if lc > sc else "SHORT"
                    matches = max(lc, sc)
                    b = bar5(inst, t)
                    if b and b["c"] > 0:
                        candidates.append((matches, -c["rank"], c, side, sigs, b["c"]))
            if candidates:
                candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
                matches, _, c, side, sigs, px = candidates[0]
                pos = {"inst": c["inst"], "rank": c["rank"], "side": side, "matches": matches, "sigs": sigs, "entry_t": t, "entry": px, "tp": px * (1 + TP) if side == "LONG" else px * (1 - TP), "sl": px * (1 - SL) if side == "LONG" else px * (1 + SL)}
                entries += 1
        t += step

    if pos:
        b = bar5(pos["inst"], end)
        if b:
            d = 1 if pos["side"] == "LONG" else -1
            trades.append({**pos, "exit_t": end, "reason": "END", "pnl": NOTIONAL * (b["c"] / pos["entry"] - 1) * d})

    wins = sum(x["reason"] == "WIN" for x in trades)
    losses = sum(x["reason"] == "LOSS" for x in trades)
    times = sum(x["reason"] == "TIME" for x in trades)
    ends_n = sum(x["reason"] == "END" for x in trades)
    total = sum(x["pnl"] for x in trades)
    wl = wins + losses
    wr = wins / wl * 100 if wl else 0.0

    print("\n===== PAGINATED MAX-HISTORY SUMMARY =====")
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
    print("NOTE: Fixed CURRENT TOP10 across history; public BloFin API has no historical TOP10 endpoint. Entry signals use closed 4H/1H/15m/5m bars. TP/SL checked on 5m bars; same-bar TP+SL counts LOSS. Fees/slippage excluded.")


if __name__ == "__main__":
    main()
