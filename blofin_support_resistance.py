"""Read-only confirmed swing support/resistance entry guard for BloFin LIVE.

Only candles closed at or before the entry signal are considered. A swing
high/low has two earlier and two later *closed* candles on its sides.
"""
import math

NEAR_LEVEL_PCT = 0.3
LOOKBACK_MINUTES = 8 * 60
CONFIRMATION_BARS = 2


def confirmed_levels(bars, signal_close_ms, timeframe_ms):
    """Return confirmed pivot highs/lows in the preceding eight hours.

    Bars must be sorted oldest to newest. The signal-close candle is allowed,
    but neither live/future candles nor unconfirmed pivots can form a level.
    """
    end_ms = int(signal_close_ms)
    interval_ms = int(timeframe_ms)
    if end_ms <= 0 or interval_ms <= 0:
        raise ValueError("Invalid signal close or timeframe")
    cutoff_ms = end_ms - LOOKBACK_MINUTES * 60 * 1000
    recent = []
    for bar in bars:
        if "close_ms" in bar:
            ts = int(bar["close_ms"])
        else:
            ts = int(bar["ts"]) + interval_ms
        if not (cutoff_ms < ts <= end_ms):
            continue
        high, low = float(bar["h"]), float(bar["l"])
        if not all(math.isfinite(v) and v > 0 for v in (high, low)) or low > high:
            raise ValueError("Invalid candle high/low")
        if recent and ts <= recent[-1][0]:
            raise ValueError("Unsorted or duplicate candle timestamps")
        recent.append((ts, high, low))

    wing = CONFIRMATION_BARS
    if len(recent) < 2 * wing + 1 or recent[-1][0] != end_ms:
        raise ValueError("Insufficient confirmed 5m candle history")

    resistance, support = set(), set()
    for i in range(wing, len(recent) - wing):
        neighbors = recent[i-wing:i+wing+1]
        if any(neighbors[j+1][0] - neighbors[j][0] != interval_ms
               for j in range(len(neighbors)-1)):
            continue
        high, low = recent[i][1], recent[i][2]
        others = neighbors[:wing] + neighbors[wing+1:]
        if all(high > row[1] for row in others):
            resistance.add(high)
        if all(low < row[2] for row in others):
            support.add(low)

    return {
        "resistance": sorted(resistance),
        "support": sorted(support),
        "signal_close_ms": end_ms,
        "lookback_minutes": LOOKBACK_MINUTES,
        "confirmation_bars": wing,
    }


def check_entry(levels, side, price):
    """Block LONG <=0.3% below resistance and SHORT <=0.3% above support.

    Fail closed for missing/invalid levels or quote; no pivots is a valid
    observation (nothing nearby to block). Do not modify any broker orders.
    """
    if side not in ("LONG", "SHORT"):
        return {"allowed": False, "reason": "SR_INVALID_SIDE"}
    if not isinstance(levels, dict) or "error" in levels:
        return {"allowed": False, "reason": "SR_LEVELS_UNAVAILABLE",
                "details": str(levels.get("error", "missing")) if isinstance(levels, dict) else "missing"}
    key = "resistance" if side == "LONG" else "support"
    if not isinstance(levels.get(key), list):
        return {"allowed": False, "reason": "SR_LEVELS_UNAVAILABLE"}
    try:
        price = float(price)
        values = [float(v) for v in levels[key]]
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid entry price")
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("Invalid support/resistance levels")
    except (ValueError, TypeError, OverflowError) as exc:
        return {"allowed": False, "reason": "SR_DATA_INVALID", "details": str(exc)}

    ahead = [v for v in values if v >= price] if side == "LONG" else [v for v in values if v <= price]
    level = (min(ahead) if side == "LONG" else max(ahead)) if ahead else None
    gap_pct = (abs(level-price)/price*100.0) if level is not None else None
    blocked = level is not None and gap_pct <= NEAR_LEVEL_PCT + 1e-10
    return {
        "allowed": not blocked,
        "reason": ("NEAR_RESISTANCE" if side == "LONG" else "NEAR_SUPPORT") if blocked else "CLEAR",
        "reference_price": price,
        "level": level,
        "distance_pct": gap_pct,
        "min_distance_pct": NEAR_LEVEL_PCT,
    }
