"""Read-only historical comparison of 24h gain and momentum for LIVE SMA 5m trades.

Uses only public BloFin market candles and existing recorded trade outcomes.
No broker credentials, no trading actions, no edits to LIVE state.
24h momentum is a CLOSED-5m-CANDLE proxy for ticker last/open24h.
"""
import bisect
import collections
import concurrent.futures
import datetime as dt
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

STATE_FILE = "blofin_live_state.json"
BASE = "https://openapi.blofin.com/api/v1/market/candles"
STEP = 300_000
HOUR = 3_600_000
THRESHOLDS = [-10, 0, 2, 5, 7.5, 10, 12.5, 15, 20, 25, 30, 40, 50, 75, 100]
BINS = [-math.inf, 0, 5, 10, 15, 20, 30, 50, 100, math.inf]


def fetch(inst, earliest_ms):
    bars = {}
    after = None
    seen = set()
    for page in range(10):
        args = {"instId": inst, "bar": "5m", "limit": "1440"}
        if after is not None:
            args["after"] = str(after)
        url = BASE + "?" + urllib.parse.urlencode(args)
        err = None
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "BloFinPublicHistoricalTest/1.0"})
                with urllib.request.urlopen(request, timeout=25) as response:
                    payload = json.load(response)
                if str(payload.get("code")) != "0":
                    raise ValueError(f"Exchange code={payload.get('code')}, msg={payload.get('msg')}")
                data = payload.get("data") or []
                if not data:
                    raise RuntimeError("Exchange returned no candle rows")
                break
            except (urllib.error.URLError, OSError, TimeoutError, ValueError, RuntimeError) as exc:
                err = exc
                if attempt < 3:
                    time.sleep(1.0 + attempt)
        else:
            raise RuntimeError(f"{inst}: public data fetch failed: {type(err).__name__}: {err}")
        oldest = None
        for row in data:
            timestamp = int(row[0])
            oldest = min(timestamp, oldest) if oldest is not None else timestamp
            if timestamp <= earliest_ms and timestamp not in bars:
                # Retain eligible completed candles; rows include confirm at last index.
                pass
            if str(row[-1]) == "1":
                bars[timestamp + STEP] = float(row[4])
        if oldest is None:
            break
        if oldest in seen:
            raise RuntimeError(f"{inst}: repeated pagination cursor {oldest}")
        seen.add(oldest)
        if oldest <= earliest_ms - STEP:
            break
        after = oldest
    return inst, bars


def metric(trades):
    gross = sum(float(t["gross_pnl_usdt"]) for t in trades)
    net = sum(float(t["net_pnl_usdt"]) for t in trades)
    wins = sum(float(t["net_pnl_usdt"]) > 0 for t in trades)
    return {
        "n": len(trades), "wins": wins, "losses": len(trades) - wins,
        "win_rate_pct": round(100 * wins / len(trades), 1) if trades else None,
        "gross_usdt": round(gross, 4), "net_usdt": round(net, 4),
        "longs": sum(t["side"] == "LONG" for t in trades),
        "shorts": sum(t["side"] == "SHORT" for t in trades),
    }


def rows_for_cutoff(trades, key):
    return [
        {"min": n, **metric([x for x in trades if x[key] >= n])}
        for n in THRESHOLDS
    ]


def pct(current, older):
    return 100.0 * (current / older - 1.0) if older > 0 else None


def main():
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    original = [
        x for x in state["trade_history"]
        if str(x.get("strategy", "")).startswith("SMA10_SMA20_")
        and str(x.get("strategy", "")).endswith("_5m")
        and 1 <= int(x.get("signal_rank") or 0) <= 7
    ]
    required = {}
    for t in original:
        anchor = int(t.get("signal_close_ms") or t["opened_ms"] // STEP * STEP)
        required[t["inst"]] = min(required.get(t["inst"], anchor), anchor)
    print("original_trade_count", len(original), "instruments", len(required), flush=True)
    print("oldest", dt.datetime.fromtimestamp(min(required.values()) / 1000, dt.timezone.utc), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {
            pool.submit(fetch, inst, minimum - 25 * HOUR): inst
            for inst, minimum in required.items()
        }
        fetched = {}
        for task in concurrent.futures.as_completed(tasks):
            inst = tasks[task]
            try:
                _, candles = task.result()
                fetched[inst] = candles
                print("DATA", inst, "bars", len(candles),
                      "first", dt.datetime.fromtimestamp(min(candles) / 1000, dt.timezone.utc).isoformat()
                      if candles else "none", flush=True)
            except Exception as exc:
                print("DATA_ERROR", inst, str(exc), flush=True)
                fetched[inst] = {}
    augmented, skipped = [], []
    for t in original:
        closes = fetched.get(t["inst"], {})
        anchor = int(t.get("signal_close_ms") or (int(t["opened_ms"]) // STEP) * STEP)
        # For no future-peeking use last fully confirmed close at or before signal.
        times = sorted(closes)
        i = bisect.bisect_right(times, anchor) - 1
        if i < 0:
            skipped.append([t["inst"], anchor, "no candle before signal"])
            continue
        cur_time = times[i]
        cur_price = closes[cur_time]
        result = dict(t)
        result["source_bar_close_ms"] = cur_time
        any_missing = False
        for hours, key in [(24, "change24h"), (4, "change4h"), (1, "change1h")]:
            target = cur_time - hours * HOUR
            reference = closes.get(target)
            # Require exact aligned reference (no neighboring / future candle substitutions).
            if reference is None:
                skipped.append([t["inst"], anchor, f"missing exact {hours}h reference"])
                any_missing = True
                break
            result[key] = pct(cur_price, reference)
        if not any_missing:
            augmented.append(result)
    print("MATCHED", len(augmented), "SKIPPED", len(skipped))
    for item in skipped:
        print("SKIPPED", *item)
    if len(augmented) < len(original):
        print("WARNING incomplete candle coverage: results are incomplete")
    rank12 = [t for t in augmented if t["signal_rank"] in [1, 2]]
    rank37 = [t for t in augmented if t["signal_rank"] in [3, 4, 5, 6, 7]]
    print("BASELINE", json.dumps({"rank12": metric(rank12), "rank37": metric(rank37), "all": metric(augmented)}, sort_keys=True))
    for name, subset in [("RANK12", rank12), ("RANK37", rank37), ("ALL", augmented), ("RANK12_LONG", [x for x in rank12 if x["side"]=="LONG"]), ("RANK12_SHORT", [x for x in rank12 if x["side"]=="SHORT"])]:
        print("THRESHOLDS_24H", name, json.dumps(rows_for_cutoff(subset, "change24h"), sort_keys=True))
        print("THRESHOLDS_4H", name, json.dumps(rows_for_cutoff(subset, "change4h"), sort_keys=True))
        print("THRESHOLDS_1H", name, json.dumps(rows_for_cutoff(subset, "change1h"), sort_keys=True))
    for left, right in zip(BINS, BINS[1:]):
        subset = [x for x in rank12 if left <= x["change24h"] < right]
        print("BUCKET_24H", str(left), str(right), json.dumps(metric(subset),sort_keys=True))
    for t in sorted(rank12, key=lambda x: x["opened_ms"]):
        print("TRADE", json.dumps({
            "inst": t["inst"], "rank": t["signal_rank"], "side": t["side"],
            "signal_utc": dt.datetime.fromtimestamp(t["signal_close_ms"]/1000, dt.timezone.utc).isoformat(),
            "change24h": round(t["change24h"],3), "change4h": round(t["change4h"],3),
            "change1h": round(t["change1h"],3),
            "gross_pnl_usdt": round(float(t["gross_pnl_usdt"]),5),
            "net_pnl_usdt": round(float(t["net_pnl_usdt"]),5)},sort_keys=True))


if __name__ == "__main__":
    main()
