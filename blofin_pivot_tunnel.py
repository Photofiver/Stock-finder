"""Closed 5m four-pivot price tunnel. No network, no orders, no future candles."""

import math

FIVE = 300_000


def _point(ts, price, confirmed_close_ms):
    return {"open_ms": int(ts), "price": float(price),
            "confirmed_close_ms": int(confirmed_close_ms)}


def confirmed_pivots(rows, expected_close_ms, after_ms=0):
    """rows: {5m open timestamp: (open, high, low, close, volume)}.

    A peak is high >= preceding high and > following high.
    A trough is low <= preceding low and < following low.
    The point is confirmed only when the following bar has CLOSED.
    """
    end = int(expected_close_ms)
    eligible = sorted(ts for ts in rows if ts + FIVE <= end)
    highs, lows = [], []
    for ts in eligible:
        if ts <= int(after_ms):
            continue
        prev, now, nxt = rows.get(ts-FIVE), rows.get(ts), rows.get(ts+FIVE)
        if prev is None or now is None or nxt is None or ts + 2*FIVE > end:
            continue
        if not all(math.isfinite(float(x)) and float(x) > 0
                   for x in (prev[1], prev[2], now[1], now[2], nxt[1], nxt[2])):
            continue
        if now[1] >= prev[1] and now[1] > nxt[1]:
            highs.append(_point(ts, now[1], ts+2*FIVE))
        if now[2] <= prev[2] and now[2] < nxt[2]:
            lows.append(_point(ts, now[2], ts+2*FIVE))
    return highs, lows


def _line(a, b, t):
    if a["open_ms"] >= b["open_ms"]:
        raise ValueError("Anchor order invalid")
    return a["price"] + (b["price"]-a["price"]) * (
        (int(t)-a["open_ms"])/(b["open_ms"]-a["open_ms"]))


def evaluate_tunnel(rows, expected_close_ms, after_ms=0):
    """Only a newly confirmed FOURTH distinct point can create an entry signal."""
    end = int(expected_close_ms)
    outcome = {
        "status": "UNAVAILABLE", "reason": "NOT_ENOUGH_CONFIRMED_POINTS",
        "rule": "PIVOT_HIGH_LOW_ONE_LEFT_ONE_RIGHT_V1",
        "timeframe": "5m", "signal_close_ms": end,
        "entry_allowed": False, "breakout_confirmed": False,
    }
    candle = rows.get(end-FIVE)
    if candle is None:
        outcome["reason"] = "SIGNAL_CANDLE_NOT_CONFIRMED"
        return outcome
    highs, lows = confirmed_pivots(rows, end, after_ms)
    if len(highs) < 2 or len(lows) < 2:
        return outcome
    h1, h2 = highs[-2:]
    l1, l2 = lows[-2:]
    if len({p["open_ms"] for p in (h1,h2,l1,l2)}) != 4:
        outcome["reason"] = "FOUR_DISTINCT_SWINGS_REQUIRED"
        return outcome
    if h2["price"] < h1["price"] and l2["price"] < l1["price"]:
        side = "SHORT"
    elif h2["price"] > h1["price"] and l2["price"] > l1["price"]:
        side = "LONG"
    else:
        outcome["reason"] = "SWINGS_NOT_SAME_DIRECTION"
        return outcome
    try:
        common_start = max(h1["open_ms"], l1["open_ms"])
        upper_start, lower_start = _line(h1, h2, common_start), _line(l1,l2,common_start)
        upper, lower = _line(h1,h2,end), _line(l1,l2,end)
    except (ZeroDivisionError, ValueError):
        outcome["reason"] = "BAD_ANCHOR_GEOMETRY"
        return outcome
    if not all(math.isfinite(p) and p > 0
               for p in (upper_start,lower_start,upper,lower)):
        outcome["reason"] = "INVALID_LINE_PRICE"
        return outcome
    if upper_start <= lower_start or upper <= lower:
        outcome["reason"] = "TUNNEL_LINES_CROSS_OR_TOUCH"
        return outcome
    if not math.isfinite(float(candle[3])) or float(candle[3]) <= 0:
        outcome["reason"] = "INVALID_SIGNAL_CLOSE"
        return outcome
    newly_completed = max(h2["confirmed_close_ms"],l2["confirmed_close_ms"]) == end
    inside = lower <= float(candle[3]) <= upper
    outcome.update({
        "status": "VALID",
        "reason": ("PRICE_OUTSIDE_TUNNEL" if not inside else
                   "FOUR_POINTS_CONFIRMED_EARLIER" if not newly_completed else
                   "FOUR_POINTS_CONFIRMED_INSIDE"),
        "tunnel_direction": side,
        "upper_price": upper, "lower_price": lower,
        "upper_anchors": [h1,h2], "lower_anchors": [l1,l2],
        "last_confirmed_high": h2, "last_confirmed_low": l2,
        "fourth_confirmed_close_ms": max(h2["confirmed_close_ms"],l2["confirmed_close_ms"]),
        "signal_close_price": float(candle[3]),
        "entry_allowed": newly_completed and inside,
    })
    return outcome


def pivot_exit_transition(position_tunnel, rows, expected_close_ms, side):
    """A 5m CLOSE breaks the last confirmed LOWER HIGH (short) or
    HIGHER LOW (long). Wicks cannot close the tunnel. New same-direction
    pivots may tighten the threshold, never loosen it.
    """
    close_ms = int(expected_close_ms)
    candle = rows.get(close_ms-FIVE)
    if candle is None or side not in ("LONG","SHORT"):
        return False
    last_high = position_tunnel.get("last_confirmed_high") or {}
    last_low = position_tunnel.get("last_confirmed_low") or {}
    highs,lows = confirmed_pivots(rows,close_ms)
    if side == "SHORT":
        if not last_high.get("open_ms") or not last_high.get("price"):
            return False
        for peak in highs:
            if peak["confirmed_close_ms"] == close_ms and peak["open_ms"] > last_high["open_ms"]:
                if peak["price"] < last_high["price"]:
                    position_tunnel["last_confirmed_high"] = peak
                    last_high = peak
        return float(candle[3]) > float(last_high["price"])
    if not last_low.get("open_ms") or not last_low.get("price"):
        return False
    for trough in lows:
        if trough["confirmed_close_ms"] == close_ms and trough["open_ms"] > last_low["open_ms"]:
            if trough["price"] > last_low["price"]:
                position_tunnel["last_confirmed_low"] = trough
                last_low = trough
    return float(candle[3]) < float(last_low["price"])
