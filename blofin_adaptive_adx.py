"""Conservative, walk-forward ADX filter for LIVE SMA10/SMA20 trades.

Only completed trades of THIS strategy version are used for learning.
Learning never increases the historical ADX<38 cap: rejected trades have no
observed realized P&L, so loosening it would be unsupported extrapolation.
Each direction trains independently; no fees are used in fitting.
"""
import math

VERSION = "adaptive_adx_v1"
FALLBACK_LIMIT = 38.0
MAX_TRAINING_TRADES = 80
VALIDATION_TRADES = 8
MIN_TRAINING_TRADES = 16
MIN_WIN_TRAIN = 4
MIN_LOSS_TRAIN = 4
LIMIT_CANDIDATES = (26.0, 30.0, 34.0, 38.0)
TREND_CANDIDATES = ("ANY", "RISING", "FALLING")


def _number(value):
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (ValueError, TypeError, OverflowError):
        return None


def eligible_results(history, side):
    """Trade outcomes known when the scan starts; no future/inferred fills."""
    if side not in ("LONG", "SHORT"):
        return []
    results = []
    for row in history or []:
        if not isinstance(row, dict) or row.get("side") != side:
            continue
        if row.get("strategy") != f"SMA10_SMA20_{side}_10m":
            continue
        snap = row.get("signal_snapshot") or {}
        if not isinstance(snap, dict) or snap.get("adx_filter_version") != VERSION:
            continue
        adx = _number(snap.get("adx14"))
        prev = _number(snap.get("adx14_previous"))
        gross = _number(row.get("gross_pnl_usdt"))
        notional = _number(row.get("notional_usdt"))
        opened = row.get("opened_ms")
        closed = row.get("closed_ms")
        if (adx is None or prev is None or notional is not None and notional <= 0
                or gross is None or notional is None or not opened or not closed):
            continue
        if not (0 <= adx <= 100 and 0 <= prev <= 100):
            continue
        pnl = gross / notional * 100
        # Pathological/incomplete realized P&L records must not dominate learning.
        if not math.isfinite(pnl) or abs(pnl) > 25:
            continue
        results.append({
            "adx": adx,
            "delta": adx - prev,
            "pnl_pct": pnl,
            "opened_ms": int(opened),
        })
    results.sort(key=lambda x: x["opened_ms"])
    return results[-(MAX_TRAINING_TRADES + VALIDATION_TRADES):]


def _match(item, limit, trend):
    if item["adx"] >= limit:
        return False
    if trend == "RISING":
        return item["delta"] > 0
    if trend == "FALLING":
        return item["delta"] < 0
    return True


def _sum_pnl(rows, limit, trend):
    return sum(r["pnl_pct"] for r in rows if _match(r, limit, trend))


def policy_for_side(history, side):
    """Return a fixed fallback until a candidate clears unseen-validation tests."""
    rows = eligible_results(history, side)
    fallback = {
        "version": VERSION,
        "side": side,
        "limit": FALLBACK_LIMIT,
        "trend": "ANY",
        "status": "FALLBACK_INSUFFICIENT_OR_UNVALIDATED_HISTORY",
        "usable_completed_trades": len(rows),
        "minimum_required": MIN_TRAINING_TRADES + VALIDATION_TRADES,
    }
    if len(rows) < MIN_TRAINING_TRADES + VALIDATION_TRADES:
        return fallback
    train, validate = rows[:-VALIDATION_TRADES], rows[-VALIDATION_TRADES:]
    wins = [r for r in train if r["pnl_pct"] > 0]
    losses = [r for r in train if r["pnl_pct"] < 0]
    if len(wins) < MIN_WIN_TRAIN or len(losses) < MIN_LOSS_TRAIN:
        fallback["status"] = "FALLBACK_NOT_ENOUGH_WINS_AND_LOSSES"
        return fallback
    base_train = _sum_pnl(train, FALLBACK_LIMIT, "ANY")
    base_validate = _sum_pnl(validate, FALLBACK_LIMIT, "ANY")
    options = []
    for limit in LIMIT_CANDIDATES:
        for trend in TREND_CANDIDATES:
            if limit == FALLBACK_LIMIT and trend == "ANY":
                continue
            accepted = [r for r in train if _match(r, limit, trend)]
            winning_accepted = sum(_match(r, limit, trend) for r in wins)
            # Do not call a strategy "better" just because it trades almost never.
            if len(accepted) < max(10, math.ceil(len(train) * 0.60)):
                continue
            if winning_accepted < math.ceil(len(wins) * 0.70):
                continue
            improvement = _sum_pnl(train, limit, trend) - base_train
            if improvement > 0.5:
                options.append((improvement, len(accepted), limit, trend))
    if not options:
        fallback["status"] = "FALLBACK_NO_TRAINING_IMPROVEMENT"
        return fallback
    # Candidate selection uses TRAIN only; validation has no say in the ranking.
    options.sort(key=lambda z: (z[0], z[1], z[2]), reverse=True)
    train_improvement, train_count, limit, trend = options[0]
    validated = [r for r in validate if _match(r, limit, trend)]
    validation_wins = [r for r in validate if r["pnl_pct"] > 0]
    kept_wins = sum(_match(r, limit, trend) for r in validation_wins)
    validate_improvement = _sum_pnl(validate, limit, trend) - base_validate
    if (len(validated) < 5 or
            (validation_wins and kept_wins < math.ceil(len(validation_wins) * 0.70)) or
            validate_improvement < 0.25):
        fallback["status"] = "FALLBACK_HELD_OUT_VALIDATION_FAILED"
        return fallback
    return {
        "version": VERSION,
        "side": side,
        "limit": limit,
        "trend": trend,
        "status": "VALIDATED_ADAPTIVE",
        "usable_completed_trades": len(rows),
        "minimum_required": MIN_TRAINING_TRADES + VALIDATION_TRADES,
        "training_n": len(train),
        "validation_n": len(validate),
        "training_gross_pnl_improvement_pct_sum": round(train_improvement, 4),
        "validation_gross_pnl_improvement_pct_sum": round(validate_improvement, 4),
        "training_accepted_n": train_count,
        "validation_accepted_n": len(validated),
    }


def decision_for_signal(history, signal):
    """Apply the policy to the latest CLOSED candle; return reason and context."""
    side = signal.get("side")
    policy = policy_for_side(history, side)
    adx = _number(signal.get("adx14"))
    previous = _number(signal.get("adx14_previous"))
    trend = policy["trend"]
    if adx is None or not (0 <= adx <= 100):
        allowed, reason = False, "ADX_MISSING"
    elif adx >= policy["limit"]:
        allowed, reason = False, "ADX_AT_OR_ABOVE_LIMIT"
    elif trend != "ANY" and previous is None:
        allowed, reason = False, "ADX_PREVIOUS_MISSING"
    elif trend == "RISING" and not (adx > previous):
        allowed, reason = False, "ADX_NOT_RISING"
    elif trend == "FALLING" and not (adx < previous):
        allowed, reason = False, "ADX_NOT_FALLING"
    else:
        allowed, reason = True, "ADX_ALLOWED"
    return {
        "allowed": allowed,
        "reason": reason,
        "adx14": adx,
        "adx14_previous": previous,
        "adx_delta": (adx - previous) if adx is not None and previous is not None else None,
        **policy,
    }
