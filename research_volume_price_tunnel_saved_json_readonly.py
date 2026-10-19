"""READ-ONLY retrospective tunnel filter replay from SAVED GitHub JSON files.

Never queries exchange, accesses credentials, posts orders, or changes LIVE
strategy/state. Uses ONLY matching, time-verified archived closed 5m OHLCV
from live_watch_history_5m*.json plus actual completed trade outcomes.
Historical trade filtering is not an independent profitability forecast.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone
import gc
import json
import math

import blofin_volume_flip_both_live as live

FIVE = 300_000
FOLDER = Path("blofin_live_scans")
STATE_FILE = Path("blofin_live_state.json")
STRATEGY = live.STRATEGY


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def safe_row(o, h, low, c, v):
    try:
        o, h, low, c, v = map(float, (o, h, low, c, v))
        if (not all(map(math.isfinite, (o, h, low, c, v)))
                or o <= 0 or low <= 0 or min(o, c) < low
                or max(o, c) > h or v < 0):
            return None
        return o, h, low, c, v
    except (ValueError, TypeError):
        return None


def recorded_candle(item, scan_close_ms):
    # The archived candle itself can be stale: use it ONLY when its
    # own independently recorded close matches the original scan time.
    try:
        own_close = int(item.get("signal_close_ms") or 0)
        if own_close != scan_close_ms:
            return None
        b = item["candle"]
        value = safe_row(b["open"], b["high"], b["low"], b["close"], b["volume"])
        return (own_close - FIVE, value) if value else None
    except (KeyError, TypeError, ValueError):
        return None


def reviewed_next_candle(item, scan_close_ms):
    # The audited following candle has OHLC plus its own 5m volume.
    n = item.get("next_candle_review") or {}
    if n.get("status") != "DONE":
        return None
    try:
        actual_close = int(n.get("next_candle_close_ms") or 0)
        if actual_close != scan_close_ms + FIVE:
            return None
        ind = n.get("indicators_after_5m") or {}
        row = safe_row(n["open"], n["high"], n["low"], n["close"], ind["volume"])
        return (actual_close - FIVE, row) if row else None
    except (KeyError, TypeError, ValueError):
        return None


def entry_preexisting_filters(t):
    """Optional rough comparison; NOT a full broker/market replay."""
    s = t.get("signal_snapshot") or {}
    try:
        vol, ma = float(s["signal_volume"]), float(s["signal_mean20_prev"])
        if not ma or not (.9 <= vol / ma < 1):
            return False
        if t["side"] == "LONG":
            rank = int(s.get("rank") or t.get("signal_rank") or 0)
            return (
                1 <= rank <= 50
                and float(s["previous_mean20_prev"]) > ma
            )
        if t["side"] == "SHORT":
            quote = s.get("pre_order_quote") or {}
            return (
                float(quote["adverse_gap_pct"]) >= .05
                and float(quote["spread_pct"]) <= .05
            )
    except (ValueError, TypeError, KeyError):
        return False
    return False


def main():
    state = json.loads(STATE_FILE.read_text())
    trades = [
        x for x in state.get("trade_history", [])
        if x.get("result") in ("WIN", "LOSS")
        and x.get("side") in ("LONG", "SHORT")
    ]
    last60 = trades[-60:]
    eligible = [
        x for x in last60
        if x.get("strategy") == STRATEGY
        and (x.get("signal_snapshot") or {}).get("signal_close_ms")
    ]
    # Restrict all historical data processed to necessary pre-entry time.
    targets = defaultdict(list)
    for t in eligible:
        s = t["signal_snapshot"]
        targets[t["inst"]].append(int(s["signal_close_ms"]))
    bars = defaultdict(dict)
    conflicting = set()
    archives = sorted(FOLDER.glob("live_watch_history_5m*.json"))
    if len(archives) < 10:
        raise RuntimeError("Archived 5m JSON shards not checked out")

    read_scans = 0
    review_candles = 0
    self_candles = 0
    for index, path in enumerate(archives):
        dataset = json.loads(path.read_text(encoding="utf-8"))
        for scan in dataset.get("scans") or []:
            at = int(scan.get("signal_close_ms") or 0)
            if not at:
                continue
            # A trade needs from T-22*FIVE through the T signal candle.
            for item in scan.get("instruments") or []:
                coin = item.get("inst")
                if coin not in targets:
                    continue
                if not any(t - 25*FIVE <= at <= t+FIVE for t in targets[coin]):
                    continue
                for method in (recorded_candle, reviewed_next_candle):
                    row = method(item, at)
                    if row is None:
                        continue
                    ts, values = row
                    key = (coin, ts)
                    old = bars[coin].get(ts)
                    if old is not None and any(
                        not math.isclose(x,y,rel_tol=1e-8,abs_tol=1e-12)
                        for x,y in zip(old, values)
                    ):
                        conflicting.add(key)
                        bars[coin].pop(ts, None)
                        continue
                    if key in conflicting:
                        continue
                    bars[coin][ts] = values
                    if method == recorded_candle:
                        self_candles += 1
                    else:
                        review_candles += 1
            read_scans += 1
        del dataset
        gc.collect()
        if (index+1) % 5 == 0 or index+1 == len(archives):
            print("PROGRESS_JSON_ARCHIVES=" + json.dumps({
                "files_read":index+1,"files_total":len(archives),
                "candles_unique":sum(map(len,bars.values())),
                "scans":read_scans}), flush=True)

    rows = []
    for t in eligible:
        inst = t["inst"]
        s = t["signal_snapshot"]
        signal_time = int(s["signal_close_ms"])
        recent = [
            {"ts":ts, "o":values[0], "h":values[1],
             "l":values[2], "c":values[3], "v":values[4]}
            for ts, values in bars.get(inst, {}).items()
            if signal_time - 24*FIVE <= ts < signal_time
        ]
        valid = live.volume_price_tunnel_from_bars(recent,signal_time,t["side"])
        info = {
            "inst":inst,"side":t["side"],"result":t["result"],
            "time":utc(signal_time),"archive_candles":len(recent),
            "status":valid["status"],"reason":valid.get("reason"),
            "passed_tunnel":bool(valid["breakout_confirmed"]),
            "passed_old_entry_filters_approx":entry_preexisting_filters(t),
            "tunnel_upper":valid.get("upper_price"),
            "tunnel_lower":valid.get("lower_price"),
            "net_pnl_usdt":t.get("net_pnl_usdt"),
        }
        rows.append(info)

    def tally(items):
        items = list(items)
        nwin = sum(x["result"]=="WIN" for x in items)
        return {"count":len(items),"wins":nwin,"losses":len(items)-nwin,
                "net_pnl_usdt":round(sum(float(x["net_pnl_usdt"] or 0)
                    for x in items),6)}

    available = [x for x in rows if x["status"]=="VALID"]
    passed = [x for x in available if x["passed_tunnel"]]
    both = [x for x in passed if x["passed_old_entry_filters_approx"]]
    report = {
        "method":"Replay saved 5m JSON scans against prior CLOSED candles; original historical fills and results",
        "limitations":"Only historical actual trades are filtered; skipped/untraded signals, ranking evolution, order book, and changed fill prices cannot be reconstructed. Archives may be sparse. No trades fabricated.",
        "archived_files":len(archives),
        "candles_unique":sum(map(len,bars.values())),
        "conflicted_candles_dropped":len(conflicting),
        "recorded_candle_samples":self_candles,
        "reviewed_next_candle_samples":review_candles,
        "last60_all_sides":tally(last60),
        "last60_5m_strategy":tally(eligible),
        "verifiable_price_tunnel":tally(available),
        "passing_price_tunnel":tally(passed),
        "passing_tunnel_and_recorded_old_filters":tally(both),
        "unverifiable_trades":len(eligible)-len(available),
        "rows":rows,
    }
    print("TUNNEL_REPLAY_RESULT=" + json.dumps(report,ensure_ascii=False),flush=True)


if __name__ == "__main__":
    main()
