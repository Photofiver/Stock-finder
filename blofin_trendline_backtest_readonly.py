"""Read-only historical trendline test for user's BloFin LIVE traded universe.

Never uses private exchange endpoints, GitHub secrets, trading methods or writes
to LIVE state. Strictly causal: pivot lows are known only after 2 later 5m bars.
Test: LONG on rising support bounce, SHORT on confirmed breakdown of rising
support; compared with a raw SMA10/20 crossing on the same coin/time window.
Both strategies use IDENTICAL approximate OHLC exits: 1% TP, 0.25% SL,
10-minute hold, next 5m open entry, conservative SL if both in same bar.
Not a reconstruction of real broker TP/SL, slippage, partial fills or rank.
"""
import collections
import concurrent.futures
import datetime
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

BASE = "https://openapi.blofin.com"
FIVE = 300_000
WINDOW_HOURS = 72
WARMUP_BARS = 120
LOOKBACK_PIVOT_BARS = 72
PIVOT_CONFIRM_BARS = 2
TP = 0.01
SL = 0.0025
NOTIONAL = 20.0
TOUCH_TOL = 0.0025
BREAK_TOL = 0.0010
CLIENT = requests.Session()


def parse_rows(raw):
    out = []
    for row in raw or []:
        try:
            if isinstance(row, list) and len(row) >= 9 and str(row[8]) == "1":
                out.append({"ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                            "l": float(row[3]), "c": float(row[4])})
        except (ValueError, TypeError):
            continue
    return out


def public_get(inst, after):
    for attempt in range(5):
        try:
            response = CLIENT.get(BASE + "/api/v1/market/candles",
                                  params={"instId": inst, "bar": "5m",
                                          "after": str(int(after)), "limit": "240"},
                                  timeout=25)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(1.2 * (attempt + 1))
                continue
            response.raise_for_status()
            obj = response.json()
            if str(obj.get("code")) != "0":
                raise RuntimeError("exchange API returned nonzero code")
            return parse_rows(obj.get("data"))
        except (requests.RequestException, ValueError, RuntimeError) as e:
            if attempt == 4:
                raise RuntimeError(type(e).__name__)
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError("market candles unavailable")


def fetch_bars(inst, end_ms, earliest_needed_ms):
    result = {}
    cursor = end_ms
    pages = 0
    for _ in range(8):  # max 160 hours, with room for 72h+10h warmup
        rows = public_get(inst, cursor)
        pages += 1
        if not rows:
            break
        for bar in rows:
            if bar["ts"] + FIVE <= end_ms:
                result[bar["ts"]] = bar
        oldest = min(x["ts"] for x in rows)
        if oldest < earliest_needed_ms or oldest >= cursor:
            break
        cursor = oldest
    bars = sorted(result.values(), key=lambda x: x["ts"])
    if not bars:
        raise RuntimeError("no confirmed 5m bars")
    # Only contiguous bars can be used for a causal 5m simulation.
    ranges = []
    chunk = [bars[0]]
    for bar in bars[1:]:
        if bar["ts"] - chunk[-1]["ts"] == FIVE:
            chunk.append(bar)
        else:
            if len(chunk) >= 45:
                ranges.append(chunk)
            chunk = [bar]
    if len(chunk) >= 45:
        ranges.append(chunk)
    return ranges, pages


def rise_support(bars, i, pivots):
    """Last two *confirmed* higher pivot lows; no look-ahead."""
    eligible = [p for p in pivots if p < i and i - p <= LOOKBACK_PIVOT_BARS]
    if len(eligible) < 2:
        return None
    p, q = eligible[-2:]
    if q - p < 4 or i - q > 36:
        return None
    a, b = bars[p]["l"], bars[q]["l"]
    if a <= 0 or b <= a * 1.001:
        return None
    slope = (b - a) / (q - p)
    if not 0.00002 <= slope / a <= 0.002:
        return None
    return (a + slope * (i - p), a + slope * (i - 1 - p))


def signals(bars):
    """Yield (signal time, type, direction, bar index) using closed bars."""
    lows = [b["l"] for b in bars]
    pivots = []
    for i in range(35, len(bars) - 3):
        k = i - PIVOT_CONFIRM_BARS
        # at bar i, k pivot can first be confirmed using i and older only
        if k >= 2 and all(lows[k] < lows[k+d] for d in (-2, -1, 1, 2)):
            pivots.append(k)
        cur, prev = bars[i], bars[i-1]
        support = rise_support(bars, i, pivots)
        if support:
            line, prev_line = support
            if (line > 0 and cur["l"] >= line * (1 - TOUCH_TOL)
                and cur["l"] <= line * (1 + TOUCH_TOL)
                and cur["c"] > line * 1.0002 and cur["c"] > cur["o"]
                and prev["c"] >= prev_line * (1 - TOUCH_TOL)):
                yield (cur["ts"] + FIVE, "TREND", "LONG", i)
            elif (line > 0 and prev["c"] >= prev_line * 0.9997
                  and cur["c"] < line * (1 - BREAK_TOL)
                  and cur["c"] < cur["o"]):
                yield (cur["ts"] + FIVE, "TREND", "SHORT", i)

        closes = [b["c"] for b in bars[i-20:i+1]]
        before10, before20 = sum(closes[-11:-1])/10, sum(closes[-21:-1])/20
        after10, after20 = sum(closes[-10:])/10, sum(closes[-20:])/20
        if before10 <= before20 and after10 > after20:
            yield (cur["ts"] + FIVE, "SMA", "LONG", i)
        elif before10 >= before20 and after10 < after20:
            yield (cur["ts"] + FIVE, "SMA", "SHORT", i)


def outcome(bars, signal_i, side):
    """Approximate intrabar TP/SL; ambiguous touches counted as SL (conservative)."""
    if signal_i + 2 >= len(bars):
        return None
    first = bars[signal_i + 1]
    second = bars[signal_i + 2]
    entry = first["o"]
    if entry <= 0:
        return None
    for bar in (first, second):
        tp = bar["h"] >= entry * (1+TP) if side == "LONG" else bar["l"] <= entry * (1-TP)
        sl = bar["l"] <= entry * (1-SL) if side == "LONG" else bar["h"] >= entry * (1+SL)
        if sl:  # includes same-bar TP+SL ambiguity
            return {"pct": -SL * 100, "reason": "AMBIGUOUS" if tp else "SL",
                    "end_ms": bar["ts"] + FIVE}
        if tp:
            return {"pct": TP * 100, "reason": "TP", "end_ms": bar["ts"] + FIVE}
    px = second["c"]
    pct = (px/entry-1) * 100 * (1 if side == "LONG" else -1)
    return {"pct": pct, "reason": "TIME", "end_ms": second["ts"] + FIVE}


def aggregate(events):
    returns = [e["pct"] for e in events]
    n = len(returns)
    return {"trades": n,
            "winners": sum(x > 0 for x in returns),
            "losers": sum(x < 0 for x in returns),
            "win_rate_pct": round(100*sum(x > 0 for x in returns)/n, 2) if n else None,
            "gross_pnl_usdt_at_20_notional": round(sum(returns)*NOTIONAL/100, 4),
            "mean_gross_return_pct": round(statistics.mean(returns), 4) if n else None,
            "ambiguous_counted_as_sl": sum(e["reason"] == "AMBIGUOUS" for e in events),
            "take_profit": sum(e["reason"] == "TP" for e in events),
            "stop_loss": sum(e["reason"] == "SL" for e in events),
            "time_exit": sum(e["reason"] == "TIME" for e in events)}


def main():
    # No exchange keys are required/used.
    state = json.loads(Path("blofin_live_state.json").read_text(encoding="utf-8"))
    end_ms = (int(time.time()*1000)//FIVE)*FIVE
    start_ms = end_ms - WINDOW_HOURS * 60 * 60_000
    start_symbols = []
    actual = []
    for trade in state.get("trade_history") or []:
        if not isinstance(trade, dict):
            continue
        opened = int(trade.get("opened_ms") or 0)
        rank = trade.get("signal_rank")
        if not(start_ms <= opened < end_ms):
            continue
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue
        if not (51 <= rank <= 100):  # current user's trade universe
            continue
        inst = trade.get("inst")
        if inst and inst not in start_symbols:
            start_symbols.append(inst)
        actual.append(trade)
    if not start_symbols:
        print(json.dumps({"error":"NO_ACTUAL_TRADES_IN_LAST_72H", "window_hours":WINDOW_HOURS}))
        return
    if len(start_symbols) > 100:
        raise RuntimeError("unexpected traded-symbol count >100")
    earliest = start_ms - WARMUP_BARS * FIVE

    start = time.monotonic()
    candles, errors, pages = {}, {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(fetch_bars, inst, end_ms, earliest):inst for inst in start_symbols}
        for fut in concurrent.futures.as_completed(futs):
            inst=futs[fut]
            try:
                candles[inst], pages[inst] = fut.result()
            except Exception as e:
                errors[inst]=str(e)
    print("FETCHED_COINS="+str(len(candles))+" FAILED="+str(len(errors))+
          " PAGES="+str(sum(pages.values()))+
          " SECONDS="+str(round(time.monotonic()-start,1)),flush=True)
    if len(candles) < 3:
        raise RuntimeError("Fewer than 3 instruments returned valid candles")

    all_signals = []
    map_by_inst = {}
    for inst, segments in candles.items():
        records = []
        for bars in segments:
            for close_ms, strategy, side, i in signals(bars):
                if not(start_ms <= close_ms < end_ms):
                    continue
                res = outcome(bars, i, side)
                if res:
                    all_signals.append({"inst": inst, "at_ms": close_ms,
                                        "type":strategy, "side":side, **res})
            # Every confirmed candle is available for comparison to actual
            # entries, including candles without a fresh trendline signal.
            lows = [x["l"] for x in bars]
            pivots = []
            for i in range(35, len(bars)):
                k = i - PIVOT_CONFIRM_BARS
                if k >= 2 and all(lows[k] < lows[k+d] for d in (-2,-1,1,2)):
                    pivots.append(k)
                close_ms = bars[i]["ts"] + FIVE
                if start_ms-FIVE <= close_ms < end_ms:
                    records.append((close_ms, (bars[i], rise_support(bars,i,pivots))))
        map_by_inst[inst]=dict(records)

    all_signals.sort(key=lambda z:(z["at_ms"],z["inst"],z["type"]))
    separate = {}
    single_position = {}
    for typ in ("TREND","SMA"):
        subset = [s for s in all_signals if s["type"] == typ]
        separate[typ] = aggregate(subset)
        selected, busy_until = [], -1
        for sig in subset:
            if sig["at_ms"] < busy_until:
                continue
            selected.append(sig)
            busy_until = sig["end_ms"]
        single_position[typ] = aggregate(selected)
    trend_by_side = {side:aggregate([s for s in all_signals if s["type"]=="TREND" and s["side"]==side])
                     for side in ("LONG","SHORT")}
    # Compare direction signal at actual orders without changing real PnL.
    # Exact as-observed trade outcomes; trendline snapshots strictly before entry.
    matched = []
    for trade in actual:
        inst=trade["inst"]
        opened=int(trade["opened_ms"])
        bar_close = (opened//FIVE)*FIVE
        snapshot = map_by_inst.get(inst,{}).get(bar_close)
        if snapshot is None:
            continue
        cur, support = snapshot
        close=cur["c"]
        classification="NO_RISING_TREND"
        if support:
            line, prev_line=support
            if trade.get("side")=="LONG":
                classification = "LONG_SUPPORTING" if close >= line else "LONG_BELOW_SUPPORT"
            else:
                classification = "SHORT_AFTER_BREAK" if close < line else "SHORT_ABOVE_SUPPORT"
        try:
            pnl=float(trade.get("gross_pnl_usdt"))
        except (ValueError,TypeError):
            continue
        matched.append({"inst":inst,"side":trade.get("side"),"opened_ms":opened,
                        "gross_pnl_usdt":pnl,"bucket":classification})
    breakdown = {}
    for k in sorted(set(m["bucket"] for m in matched)):
        group = [m for m in matched if m["bucket"]==k]
        breakdown[k] = {"trades":len(group),
                        "wins_gross":sum(t["gross_pnl_usdt"]>0 for t in group),
                        "gross_pnl_usdt":round(sum(t["gross_pnl_usdt"] for t in group),5)}

    report = {
        "timestamp_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "window_utc":{"start":datetime.datetime.fromtimestamp(start_ms/1000,datetime.timezone.utc).isoformat(),
                      "end":datetime.datetime.fromtimestamp(end_ms/1000,datetime.timezone.utc).isoformat()},
        "source":"Public BloFin 5m OHLC; symbol universe from actual bot trades with entry ranks 51..100; bot LIVE state read-only",
        "coins_traded":len(start_symbols),"coins_analyzed":len(candles),
        "coins_missing":errors,
        "actual_trades_in_window":len(actual),
        "actual_trade_direction_classification":breakdown,
        "signal_count_by_type":dict(collections.Counter(s["type"] for s in all_signals)),
        "all_signals":separate,
        "at_most_one_concurrent_position":single_position,
        "trend_by_direction":trend_by_side,
        "first_15_trend_signals": [x for x in all_signals if x["type"]=="TREND"][:15],
        "rules":{
            "pivot":"center bar low lower than neighbors ±2; usable only 2 bars after it closes",
            "uptrend":"last two confirmed rising pivot lows, >=4 bars apart, last <=36 bars old; bounded slope",
            "long":"green close above projected support after wick touches within 0.25%",
            "short":"red close at least 0.10% below projected support after previous candle was above",
            "entry":"next 5m candle open after confirmed signal candle",
            "exit":"first of TP 1%, SL 0.25% or 10 minutes; same-bar TP+SL -> SL (conservative)",
            "notional_usdt":NOTIONAL,"fee_included":False,
            "limitations":"NO historical changing coin ranking reconstruction; no order book/spread/slippage/fees/live SMA10 trailing; OHLC intrabar order unknown; no causal 1m candles for first-touch"
        }
    }
    Path("trendline_backtest_readonly_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("TRENDLINE_BACKTEST_RESULT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
    if errors:
        print("WARNING_PARTIAL_HISTORY="+str(len(errors)),flush=True)


if __name__ == "__main__":
    main()
