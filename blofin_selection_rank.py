"""Point-in-time TOP7 ranking. Never uses future candles or broker results.

This module only ranks candidates; it does NOT turn rejected signals into orders.
"""
from __future__ import annotations


def score_at_close(bars, side, index, signal_metrics=None):
    """Deterministic 15m quality score, for comparison (not a profit forecast)."""
    cur = bars[index]
    prev = bars[index - 1] if index > 0 else None
    close = float(cur.get("c") or 0)
    opening = float(cur.get("o") or 0)
    rsi = cur.get("rsi")
    rsi = float(rsi) if rsi is not None else None
    macd = cur.get("macd_hist")
    prev_macd = prev.get("macd_hist") if prev else None
    histogram_delta_pct = (
        ((float(macd) - float(prev_macd)) / close) * 100.0
        if macd is not None and prev_macd is not None and close > 0 else None
    )
    metrics = signal_metrics or {}
    stoch_k = metrics.get("stoch_k")
    stoch_k = float(stoch_k) if stoch_k is not None else None
    stoch_d = metrics.get("stoch_d")
    stoch_d = float(stoch_d) if stoch_d is not None else None
    direction = 1 if side == "LONG" else -1
    score = 0.0
    reasons = []

    def part(label, points):
        nonlocal score
        score += points
        reasons.append({"factor": label, "points": points})

    if (close - opening) * direction > 0:
        part("directional_candle", 1)
    if histogram_delta_pct is not None:
        if histogram_delta_pct * direction > 0:
            part("macd_histogram_improving", 2)
        else:
            part("macd_histogram_not_improving", -1)
    if rsi is not None:
        if side == "LONG":
            if 42 <= rsi <= 65:
                part("rsi_not_overbought", 2)
            elif rsi >= 67:
                part("rsi_overbought_warning", -2)
        else:
            if 35 <= rsi <= 58:
                part("rsi_not_oversold", 2)
            elif rsi <= 33:
                part("rsi_oversold_warning", -2)
    if stoch_k is not None:
        if side == "LONG":
            if stoch_k <= 75:
                part("stochastic_headroom_long", 2)
            elif stoch_k >= 90:
                part("stochastic_high_warning", -2)
        else:
            if stoch_k >= 25:
                part("stochastic_headroom_short", 1)
            elif stoch_k <= 10:
                part("stochastic_low_warning", -2)
    if stoch_k is not None and stoch_d is not None:
        if (stoch_k - stoch_d) * direction > 0:
            part("stochastic_directional", 1)
    if prev and float(prev.get("c") or 0) > 0 and close > 0:
        directional_move_pct = (close / float(prev["c"]) - 1) * 100 * direction
        if directional_move_pct >= 2.0:
            part("large_15m_move_warning", -1)
    return {
        "score": round(score, 4),
        "factors": reasons,
        "rsi14": rsi,
        "stochastic_k": stoch_k,
        "stochastic_d": stoch_d,
        "macd_histogram_delta_pct_close": histogram_delta_pct,
        "point_in_time_only": True,
    }


def rank_signals(candidates, scores):
    """Candidates remain original 4-tuples; rank never expands eligibility."""
    return sorted(
        candidates,
        key=lambda c: (
            -float(scores.get((str(c[1]), str(c[2])), {}).get("score", 0)),
            int(c[0]),
            str(c[1]),
            str(c[2]),
        ),
    )
