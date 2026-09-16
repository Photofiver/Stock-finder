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
    "1m": 60 * 1000,
}
TOP_N = 10
NOTIONAL = 10.0
TP_PCT = 0.01
SL_PCT = 0.01
MAX_HOLD_MS = 60 * 60 * 1000
REQUEST_DELAY = 0.12
MAX_RETRIES = 6
MAX_PAGES = 5000


def api_get(path, params=None):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(REQUEST_DELAY)
            r = requests.get(BASE + path, params=params, timeout=30)
            if r.status_code == 429:
                wait = max(1.0, float(r.headers.get("Retry-After", 0) or 0), 1.5 * (attempt + 1))
                time.sleep(wait)
                continue
            r.raise_for_status()
            p = r.json()
            if str(p.get("code")) != "0":
                raise RuntimeError(p)
            data = p.get("data", [])
            if not isinstance(data, list):
                raise RuntimeError("non-list data")
            return data
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


def fetch_all_candles(inst, bar):
    by_ts = {}
    after = None
    pages = 0
    while pages < MAX_PAGES:
        params = {"instId": inst, "bar": bar, "limit": "1440"}
        if after is not None:
            params["after"] = str(after)
        raw = api_get("/api/v1/market/candles", params)
        rows = parse(raw)
        pages += 1
        if not rows:
            break
        old_count = len(by_ts)
        for row in rows:
            by_ts[row["ts"]] = row
        oldest = min(row["ts"] for row in rows)
        if len(by_ts) == old_count:
            break
        if after is not None and oldest >= after:
            break
        after = oldest
        if len(rows) < 1440:
            # Some instruments return a short final page at listing boundary.
            probe = api_get(
                "/api/v1/market/candles",
                {"instId": inst, "bar": bar, "limit": "10", "after": str(oldest)},
            )
            probe_rows = parse(probe)
            added = False
            for row in probe_rows:
                if row["ts"] not in by_ts:
                    by_ts[row["ts"]] = row
                    added = True
            if not added:
                break
            after = min(row["ts"] for row in probe_rows)
    candles = sorted(by_ts.values(), key=lambda x: x["ts"])
    return candles, pages


def build_index(candles):
    closes = [x["ts"] + 60000 for x in candles]
    return closes


def last_closed_idx(close_times, t_ms):
    return bisect.bisect_right(close_times, t_ms) - 1


def tf_signal(candles, tf, t_ms):
    dur = DURATION_MS[tf]
    close_times = [x["ts"] + dur for x in candles]
    i = bisect.bisect_right(close_times, t_ms) - 1
    if i < 1:
        return None
    prev = candles[i - 1]
    cur = candles[i]
    if cur["v"] <= prev["v"]:
        return None
    if cur["c"] > cur["o"]:
        return "LONG"
    if cur["c"] < cur["o"]:
        return "SHORT"
    return None


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins = current_top10()
    print("CURRENT TOP10:", ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins))

    data = {}
    earliest = {}
    latest = {}
    total_requests = 0

    for c in coins:
        inst = c["inst"]
        data[inst] = {}
        print(f"FETCH {inst}", flush=True)
        for tf in ["1m", "5m", "15m", "1H", "4H"]:
            candles, pages = fetch_all_candles(inst, tf)
            total_requests += pages
            data[inst][tf] = candles
            if candles:
                print(f"  {tf}: {len(candles)} bars {fmt(candles[0]['ts'])} -> {fmt(candles[-1]['ts'])} pages={pages}", flush=True)
            else:
                print(f"  {tf}: no data", flush=True)
        if data[inst]["1m"]:
            earliest[inst] = data[inst]["1m"][0]["ts"] + 60000
            latest[inst] = data[inst]["1m"][-1]["ts"] + 60000

    if not earliest:
        raise RuntimeError("No 1m history available")

    global_start = min(earliest.values())
    global_end = max(latest.values())
    print(f"MAX DISCOVERED WINDOW UTC {fmt(global_start)} -> {fmt(global_end)}")
    print("EARLIEST 1m BY COIN:")
    for c in coins:
        inst = c["inst"]
        if inst in earliest:
            print(f"  #{c['rank']} {inst}: {fmt(earliest[inst])}")

    one_min_close_times = {
        inst: [x["ts"] + 60000 for x in tfdata["1m"]]
        for inst, tfdata in data.items()
    }
    tf_close_times = {
        inst: {
            tf: [x["ts"] + DURATION_MS[tf] for x in tfdata[tf]]
            for tf in TIMEFRAMES
        }
        for inst, tfdata in data.items()
    }

    def signal_at(inst, tf, t_ms):
        candles = data[inst][tf]
        times = tf_close_times[inst][tf]
        i = bisect.bisect_right(times, t_ms) - 1
        if i < 1:
            return None
        prev, cur = candles[i - 1], candles[i]
        if cur["v"] <= prev["v"]:
            return None
        if cur["c"] > cur["o"]:
            return "LONG"
        if cur["c"] < cur["o"]:
            return "SHORT"
        return None

    def close_price(inst, t_ms):
        times = one_min_close_times[inst]
        i = bisect.bisect_right(times, t_ms) - 1
        if i < 0:
            return None
        return data[inst]["1m"][i]["c"]

    def bar_ending_at_or_before(inst, t_ms):
        times = one_min_close_times[inst]
        i = bisect.bisect_right(times, t_ms) - 1
        if i < 0:
            return None
        return data[inst]["1m"][i]

    open_pos = None
    trades = []
    entries = 0
    t_ms = global_start
    minute = 60000

    while t_ms <= global_end:
        if open_pos:
            inst = open_pos["inst"]
            b = bar_ending_at_or_before(inst, t_ms)
            if b and b["ts"] + minute > open_pos["entry_t"]:
                if open_pos["side"] == "LONG":
                    hit_sl = b["l"] <= open_pos["sl"]
                    hit_tp = b["h"] >= open_pos["tp"]
                else:
                    hit_sl = b["h"] >= open_pos["sl"]
                    hit_tp = b["l"] <= open_pos["tp"]
                if hit_sl or hit_tp:
                    # Conservative rule: if both touched inside one 1m bar, count LOSS.
                    loss = hit_sl
                    reason = "LOSS" if loss else "WIN"
                    exit_px = open_pos["sl"] if loss else open_pos["tp"]
                    pnl = -NOTIONAL * SL_PCT if loss else NOTIONAL * TP_PCT
                    trades.append({**open_pos, "exit_t": t_ms, "exit": exit_px, "reason": reason, "pnl": pnl})
                    open_pos = None
            if open_pos and t_ms - open_pos["entry_t"] >= MAX_HOLD_MS:
                px = close_price(open_pos["inst"], t_ms)
                if px is not None:
                    direction = 1 if open_pos["side"] == "LONG" else -1
                    ret = (px / open_pos["entry"] - 1) * direction
                    trades.append({**open_pos, "exit_t": t_ms, "exit": px, "reason": "TIME", "pnl": NOTIONAL * ret})
                    open_pos = None

        if open_pos is None:
            candidates = []
            for c in coins:
                inst = c["inst"]
                if inst not in earliest or not (earliest[inst] <= t_ms <= latest[inst]):
                    continue
                sigs = {tf: signal_at(inst, tf, t_ms) for tf in TIMEFRAMES}
                longs = sum(s == "LONG" for s in sigs.values())
                shorts = sum(s == "SHORT" for s in sigs.values())
                if longs >= 3 or shorts >= 3:
                    side = "LONG" if longs > shorts else "SHORT"
                    matches = max(longs, shorts)
                    px = close_price(inst, t_ms)
                    if px is not None and px > 0:
                        candidates.append((matches, -c["rank"], c, side, sigs, px))
            if candidates:
                candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
                matches, _, c, side, sigs, px = candidates[0]
                if side == "LONG":
                    tp = px * (1 + TP_PCT)
                    sl = px * (1 - SL_PCT)
                else:
                    tp = px * (1 - TP_PCT)
                    sl = px * (1 + SL_PCT)
                open_pos = {
                    "inst": c["inst"],
                    "rank": c["rank"],
                    "side": side,
                    "matches": matches,
                    "sigs": sigs,
                    "entry_t": t_ms,
                    "entry": px,
                    "tp": tp,
                    "sl": sl,
                }
                entries += 1
        t_ms += minute

    if open_pos:
        px = close_price(open_pos["inst"], global_end)
        if px is not None:
            direction = 1 if open_pos["side"] == "LONG" else -1
            ret = (px / open_pos["entry"] - 1) * direction
            trades.append({**open_pos, "exit_t": global_end, "exit": px, "reason": "END", "pnl": NOTIONAL * ret})
            open_pos = None

    wins = sum(t["reason"] == "WIN" for t in trades)
    losses = sum(t["reason"] == "LOSS" for t in trades)
    timed = sum(t["reason"] == "TIME" for t in trades)
    ended = sum(t["reason"] == "END" for t in trades)
    total = sum(t["pnl"] for t in trades)
    closed_binary = wins + losses
    win_rate = (wins / closed_binary * 100) if closed_binary else 0.0
    all_positive = sum(t["pnl"] > 0 for t in trades)
    all_negative = sum(t["pnl"] < 0 for t in trades)

    print("\n===== MAX HISTORY SUMMARY =====")
    print(f"window_utc={fmt(global_start)} -> {fmt(global_end)}")
    print(f"entries={entries} closed={len(trades)}")
    print(f"wins={wins} losses={losses} time_exits={timed} end_exits={ended}")
    print(f"tp_sl_win_rate={win_rate:.2f}% ({wins}/{closed_binary})")
    print(f"all_positive={all_positive} all_negative={all_negative}")
    print(f"gross_pnl={total:+.4f} USDT on max 10 USDT notional")
    print(f"api_pages={total_requests}")

    by_coin = {}
    for tr in trades:
        s = by_coin.setdefault(tr["inst"], {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        s["n"] += 1
        s["wins"] += tr["reason"] == "WIN"
        s["losses"] += tr["reason"] == "LOSS"
        s["pnl"] += tr["pnl"]
    print("BY COIN:")
    for c in coins:
        s = by_coin.get(c["inst"])
        if s:
            print(f"  {c['inst']}: trades={s['n']} wins={s['wins']} losses={s['losses']} pnl={s['pnl']:+.4f}")

    print("LAST 20 TRADES:")
    for tr in trades[-20:]:
        status = " | ".join(f"{tf}:{tr['sigs'][tf] or '-'}" for tf in TIMEFRAMES)
        print(
            f"  {fmt(tr['entry_t'])} {tr['inst']} {tr['side']} {tr['matches']}/4 "
            f"-> {fmt(tr['exit_t'])} {tr['reason']} pnl={tr['pnl']:+.4f} | {status}"
        )

    print("NOTE: This exhaustive test uses the CURRENT BloFin TOP10 as a fixed universe across history because BloFin's public API does not provide historical TOP10 rankings. Fees/slippage are excluded. If TP and SL are both touched inside the same 1m candle, LOSS is assumed.")


if __name__ == "__main__":
    main()
