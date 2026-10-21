"""Read-only best observed price/profit during a verified BloFin position.

Use only fully closed 1m candles completely inside the actual holding period.
The entry/exit fills are also observations. Prices in the partial entry/exit
minutes are unknowable from OHLC, so this is a lower bound, not a tick-level high.
"""
import math

MINUTE_MS = 60_000


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def summarize_position_peak(pos, history, fetch_1m_candles):
    """Never places/cancels orders. Exceptions fetching market data are recorded."""
    entry = _positive(history.get("openAveragePrice") or pos.get("reference_entry"))
    exit_price = _positive(history.get("closeAveragePrice"))
    side = str(pos.get("side") or "").upper()
    opened = int(pos.get("opened_ms") or 0)
    closed = int(history.get("updateTime") or 0)
    if entry is None or exit_price is None or side not in ("LONG", "SHORT") or opened <= 0 or closed < opened:
        return {"available": False, "reason": "missing verified entry, exit, side or holding timestamps"}

    direction = 1 if side == "LONG" else -1
    first_full_minute = ((opened + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
    last_full_minute_exclusive = (closed // MINUTE_MS) * MINUTE_MS
    expected = max(0, (last_full_minute_exclusive - first_full_minute) // MINUTE_MS)
    candles = {}
    fetch_error = None
    try:
        raw = fetch_1m_candles(str(pos.get("inst") or ""), min(1440, max(80, expected + 10)))
        for row in raw if isinstance(raw, list) else []:
            try:
                if len(row) < 9 or str(row[8]) != "1":
                    continue
                ts = int(row[0])
                high, low = _positive(row[2]), _positive(row[3])
                if (high is None or low is None or low > high
                        or ts < first_full_minute or ts + MINUTE_MS > closed):
                    continue
                candles[ts] = (high, low)
            except (TypeError, ValueError, OverflowError):
                continue
    except Exception as exc:
        fetch_error = f"{type(exc).__name__}: {exc}"[:180]

    source = "confirmed_1m_full_candles_plus_verified_entry_exit_fills"
    # Old saved positions may have a 5m audit even when the 1m API is down.
    if not candles:
        audit = pos.get("volume_candle_audit") or {}
        for bar in audit.get("candles") or []:
            try:
                end = int(bar.get("close_ms") or 0)
                high, low = _positive(bar.get("high")), _positive(bar.get("low"))
                if (high is None or low is None or low > high
                        or end - 300_000 < opened or end > closed):
                    continue
                candles[end - 300_000] = (high, low)
            except (TypeError, ValueError, OverflowError, AttributeError):
                continue
        source = ("confirmed_5m_full_candles_plus_verified_fills"
                  if candles else "verified_entry_exit_fills_only")

    best_price = entry
    best_pct = 0.0
    best_at_ms = opened
    best_window = "entry_fill"
    highest = max(entry, exit_price)
    lowest = min(entry, exit_price)

    def improve(price, observed_at, observation):
        nonlocal best_price, best_pct, best_at_ms, best_window
        gain = direction * (price / entry - 1.0) * 100.0
        if gain > best_pct:
            best_pct, best_price = gain, price
            best_at_ms, best_window = observed_at, observation

    improve(exit_price, closed, "exit_fill")
    for ts in sorted(candles):
        high, low = candles[ts]
        highest = max(highest, high)
        lowest = min(lowest, low)
        improve(high if side == "LONG" else low, ts, "candle_window_start")

    notional = _positive(pos.get("notional_usdt"))
    return {
        "available": True,
        "side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "peak_profit_pct_before_exit": round(best_pct, 6),
        "peak_profit_usdt_before_exit_gross_approx": (
            round(notional * best_pct / 100.0, 8) if notional is not None else None
        ),
        "peak_favorable_coin_price": best_price,
        "highest_coin_price_observed": highest,
        "lowest_coin_price_observed": lowest,
        "peak_observed_at_ms_or_window_start": best_at_ms,
        "peak_observation_type": best_window,
        "source": source,
        "full_1m_candles_observed": len(candles) if "1m" in source else 0,
        "full_1m_candles_expected": expected,
        "full_minute_coverage": ("1m" in source and len(candles) == expected),
        "fetch_error": fetch_error,
        "precision_note": (
            "Observed maximum, not exact tick maximum: entry/exit partial minutes "
            "are excluded; candle high/low timestamp within each full minute is unknown. "
            "Gross USDT estimate excludes trading fees and funding."
        ),
    }
