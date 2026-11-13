#!/usr/bin/env python3
"""Read-only RSI(14) recovery for closed BloFin RSI89 SHORT trades.

No credentials, private API, strategy import, or order endpoints. Reads the LIVE
state and reconstructs RSI at fully confirmed 5m candle boundaries. Writes only
a standalone dashboard JSON file, never the LIVE state.
"""
import argparse
import json
import math
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FIVE = 300_000
PERIOD = 14
API = "https://openapi.blofin.com/api/v1/market/candles"
MAX_PAGES = 8
MAX_TRADES_PER_RUN = 200


def trade_key(t):
    return f"{t.get('inst', '')}|{int(t.get('opened_ms') or 0)}|{int(t.get('closed_ms') or 0)}"


def wilder_rsi(closes, period=PERIOD):
    result = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains = [max(0., closes[i] - closes[i-1]) for i in range(1, period+1)]
    losses = [max(0., closes[i-1] - closes[i]) for i in range(1, period+1)]
    avg_gain, avg_loss = sum(gains)/period, sum(losses)/period
    result[period] = 100. if avg_loss == 0 else 100. - 100./(1. + avg_gain/avg_loss)
    for i in range(period+1, len(closes)):
        diff = closes[i] - closes[i-1]
        avg_gain = (avg_gain*(period-1) + max(0., diff))/period
        avg_loss = (avg_loss*(period-1) + max(0., -diff))/period
        result[i] = 100. if avg_loss == 0 else 100. - 100./(1. + avg_gain/avg_loss)
    return result


def request_candles(inst, end_before_ts, needed_start_ts):
    """Fetch completed historic 5m candles backwards with exchange pagination."""
    cursor = end_before_ts
    by_ts = {}
    for page in range(MAX_PAGES):
        params = urlencode({"instId": inst, "bar": "5m", "after": str(cursor), "limit": "500"})
        request = Request(API+"?"+params, headers={"User-Agent": "BloFinRSIObserver/1.0", "Accept": "application/json"})
        last_error = None
        payload = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=22) as response:
                    payload = json.load(response)
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1.2 * (attempt + 1))
        if payload is None:
            raise RuntimeError(f"Market candles unavailable: {type(last_error).__name__}")
        if str(payload.get("code")) != "0" or not isinstance(payload.get("data"), list):
            raise RuntimeError(f"Market candles rejected: {str(payload.get('code'))[:24]}")
        rows = payload["data"]
        if not rows:
            break
        earliest = cursor
        for row in rows:
            try:
                ts = int(row[0])
                close = float(row[4])
                if len(row) >= 9 and str(row[8]) != "1":
                    continue
                if ts < cursor and ts >= needed_start_ts and close > 0 and math.isfinite(close):
                    by_ts[ts] = close
                if ts < earliest:
                    earliest = ts
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
        if earliest >= cursor:
            break
        if earliest <= needed_start_ts:
            break
        cursor = earliest
        time.sleep(.25)
    return by_ts


def reconstruct(trade):
    opened = int(trade.get("opened_ms") or 0)
    closed = int(trade.get("closed_ms") or 0)
    signal_close = int(trade.get("signal_close_ms") or 0)
    snap = trade.get("signal_snapshot") or {}
    entry_rsi = snap.get("rsi14")
    if not opened or not closed or closed <= opened or signal_close <= 0 or entry_rsi is None:
        return None, "MISSING_TIMES_OR_ENTRY_RSI"
    try:
        entry_rsi = float(entry_rsi)
    except (ValueError, TypeError):
        return None, "INVALID_ENTRY_RSI"
    if not math.isfinite(entry_rsi):
        return None, "INVALID_ENTRY_RSI"
    # Exact signal candle ends at signal_close. 200 bars, same as LIVE RSI89 calculation.
    signal_bar = signal_close - FIVE
    first_required = signal_bar - 199 * FIVE
    last_confirmed = (closed // FIVE) * FIVE - FIVE
    if last_confirmed < signal_bar:
        return None, "NO_CONFIRMED_CANDLE_AS_OF_EXIT"
    if (last_confirmed - first_required) // FIVE + 1 > MAX_PAGES * 500:
        return None, "HOLD_TOO_LONG_FOR_BOUNDED_FETCH"
    candles = request_candles(str(trade["inst"]), last_confirmed + FIVE, first_required)
    ts_list = list(range(first_required, last_confirmed + FIVE, FIVE))
    if any(ts not in candles for ts in ts_list):
        return None, f"INCOMPLETE_5M_CANDLES_{len(candles)}_OF_{len(ts_list)}"
    rsi = wilder_rsi([candles[ts] for ts in ts_list])
    by_time = {ts: rsi[i] for i, ts in enumerate(ts_list)}
    rebuilt_entry = by_time.get(signal_bar)
    if rebuilt_entry is None or abs(rebuilt_entry - entry_rsi) > 2:
        return None, "RSI_ENTRY_MISMATCH_WITH_LIVE_SNAPSHOT"
    observed = [entry_rsi]
    count_closed_after_entry = 0
    for ts in ts_list:
        if ts + FIVE > opened and ts + FIVE <= closed and by_time[ts] is not None:
            observed.append(by_time[ts])
            count_closed_after_entry += 1
    exit_rsi = by_time.get(last_confirmed)
    if exit_rsi is None:
        return None, "RSI_EXIT_UNAVAILABLE"
    return {
        "rsi14_min": round(min(observed), 4),
        "rsi14_max": round(max(observed), 4),
        "rsi14_exit": round(exit_rsi, 4),
        "exit_rsi_candle_closed_ms": last_confirmed + FIVE,
        "entry_rsi_live": round(entry_rsi, 4),
        "entry_rsi_rebuilt": round(rebuilt_entry, 4),
        "confirmed_5m_closes_during_position": count_closed_after_entry,
        "source": "BloFin public completed 5m candles; Wilder RSI14; signal included",
        "precision": "Only completed 5m candles, not intrabar RSI or RSI at exact broker fill time",
    }, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    output = Path(args.out)
    existing = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    completed = existing.get("trades", {})
    if not isinstance(completed, dict):
        completed = {}
    errors = {}
    candidates = [
        t for t in state.get("trade_history", [])
        if isinstance(t, dict) and t.get("side") == "SHORT"
        and (t.get("signal_snapshot") or {}).get("rsi89_short_entry") is True
        and t.get("closed_ms")
    ]
    # Persist past values; compute newly closed trades first.
    pending = [t for t in candidates if trade_key(t) not in completed]
    pending.sort(key=lambda t: int(t.get("closed_ms") or 0), reverse=True)
    count = 0
    for trade in pending[:MAX_TRADES_PER_RUN]:
        key = trade_key(trade)
        try:
            value, reason = reconstruct(trade)
            if value is not None:
                completed[key] = value
                count += 1
            else:
                errors[key] = reason
        except Exception as exc:
            errors[key] = f"{type(exc).__name__}: {str(exc)[:100]}"
    if completed or not output.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "source": "BloFin public completed 5m market candles",
            "method": "Wilder RSI14, 200-bar signal lookback",
            "calculation_note": "Intrabar extremes and exact fill-time RSI are not observable.",
            "last_updated_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "eligible_closed_trades": len(candidates),
            "computed_trades": len(completed),
            "pending_or_unavailable": len(pending) - count,
            "recent_errors": dict(list(errors.items())[:25]),
            "trades": completed,
        }
        # Avoid an unnecessary Pages deployment if there are no new valid values.
        if count > 0 or not output.exists():
            output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps({"eligible": len(candidates), "newly_computed": count, "saved": len(completed), "unavailable": len(errors), "examples": list(errors.items())[:3]}))


if __name__ == "__main__":
    main()
