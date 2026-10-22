"""Read-only post-trade analysis. This module NEVER places or modifies orders.

Each verified closed trade is recorded once in a small per-UK-day JSONL archive.
Candle extrema are research approximations, not exact executable exit prices.
"""
import json
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
ARCHIVE_DIR = os.getenv("BLOFIN_TRADE_REVIEWS_DIR", "blofin_trade_reviews")


def number(value):
    try:
        value = float(Decimal(str(value)))
        return value if math.isfinite(value) else None
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


def pct(value, digits=5):
    return round(value, digits) if value is not None and math.isfinite(value) else None


def make_review(trade):
    """Summarize verified fills, fee-inclusive result, and OBSERVED candle peak."""
    inst = str(trade.get("inst") or "")
    side = str(trade.get("side") or "")
    opened_ms = int(trade.get("opened_ms") or 0)
    closed_ms = int(trade.get("closed_ms") or 0)
    history_id = str(trade.get("history_id") or "")
    review_id = f"{history_id}:{inst}:{opened_ms}:{closed_ms}"
    entry = number(trade.get("open_price"))
    exit_price = number(trade.get("close_price"))
    gross_usdt = number(trade.get("gross_pnl_usdt"))
    net_usdt = number(trade.get("net_pnl_usdt"))
    fee_usdt = number(trade.get("fee_usdt"))
    notional = number(trade.get("notional_usdt"))
    peak = trade.get("peak_position_before_exit") or {}
    peak_available = bool(peak.get("available"))
    best_pct = number(peak.get("peak_profit_pct_before_exit")) if peak_available else None
    best_usdt = number(peak.get("peak_profit_usdt_before_exit_gross_approx")) if peak_available else None

    actual_price_pct = None
    if entry and exit_price and entry > 0 and side in ("LONG", "SHORT"):
        direction = 1 if side == "LONG" else -1
        actual_price_pct = direction * (exit_price / entry - 1) * 100
    actual_net_notional_pct = (
        net_usdt / notional * 100 if net_usdt is not None and notional and notional > 0
        else None
    )
    giveback_pct = (
        max(0.0, best_pct - actual_price_pct)
        if best_pct is not None and actual_price_pct is not None else None
    )
    estimated_gross_given_back = (
        max(0.0, best_usdt - gross_usdt)
        if best_usdt is not None and gross_usdt is not None else None
    )
    # Gross positive at some earlier candle extreme does NOT prove the trade
    # could have been closed at that price or profitably after fees.
    favorable_then_loss = bool(best_pct is not None and best_pct > 0 and net_usdt is not None and net_usdt < 0)
    reason = str(trade.get("reason") or "unknown")
    if reason.lower() == "external close":
        close_explanation = (
            "Exchange-confirmed close; exact trigger (SL/TP/manual) is not "
            "distinguished by the saved history. Do not assume SL was hit."
        )
    else:
        close_explanation = "Bot-recorded reason: " + reason

    if best_pct is None:
        observation = "No complete usable peak candle data; favorable excursion unknown."
    elif favorable_then_loss:
        observation = (
            "Observed gross profit earlier but negative realized net result. "
            "Review earlier profit protection/trailing SL; this is NOT proof a "
            "different stop could execute profitably."
        )
    elif giveback_pct is not None and giveback_pct > 0:
        observation = "Price reversed part of the observed favorable excursion before exit."
    else:
        observation = "No verified profitable-then-loss pattern in available candles."

    closed_uk = datetime.fromtimestamp(closed_ms / 1000, timezone.utc).astimezone(UK)
    opened_uk = datetime.fromtimestamp(opened_ms / 1000, timezone.utc).astimezone(UK)
    return {
        "version": 1, "review_id": review_id, "history_id": history_id,
        "instrument": inst, "side": side, "result": str(trade.get("result") or ""),
        "opened_uk": opened_uk.isoformat(timespec="seconds"),
        "closed_uk": closed_uk.isoformat(timespec="seconds"),
        "opened_ms": opened_ms, "closed_ms": closed_ms,
        "duration_minutes": number(trade.get("hold_minutes")),
        "entry_price": entry, "exit_price": exit_price,
        "saved_sl": number(trade.get("sl")), "saved_broker_tp": number(trade.get("tp")),
        "gross_usdt": gross_usdt, "fees_signed_usdt": fee_usdt,
        "net_usdt": net_usdt, "net_return_on_notional_pct": pct(actual_net_notional_pct),
        "actual_directional_price_pct": pct(actual_price_pct),
        "best_observed_gross_pct_approx": pct(best_pct),
        "best_observed_gross_usdt_approx": best_usdt,
        "best_observed_favorable_price_approx": (
            number(peak.get("peak_favorable_coin_price")) if peak_available else None),
        "best_observed_at_uk": (
            datetime.fromtimestamp(
                int(peak["peak_observed_at_ms_or_window_start"]) / 1000, timezone.utc
            ).astimezone(UK).isoformat(timespec="seconds")
            if peak_available and peak.get("peak_observed_at_ms_or_window_start") else None
        ),
        "observed_giveback_price_pct_points_approx": pct(giveback_pct),
        "observed_gross_given_back_usdt_approx": pct(estimated_gross_given_back, 8),
        "had_gross_profit_before_net_loss": favorable_then_loss,
        "close_reason_recorded": reason,
        "close_explanation": close_explanation,
        "observation": observation,
        "peak_data_available": peak_available,
        "peak_data_full_minute_coverage": bool(peak.get("full_minute_coverage")),
        "peak_source": str(peak.get("source") or ""),
        "caveat": (
            "1m high/low order within each candle is unknown; actual peak can differ. "
            "Gross theoretical peak omits exit slippage, funding and hypothetical "
            "execution fees. Journal and alerts are observational only."
        ),
        "entry_chart": str(trade.get("entry_chart") or ""),
        "exit_chart": str(trade.get("exit_chart") or ""),
    }


def archive_review(review, directory=None):
    """Idempotent daily journal: same exchange close is never appended twice."""
    folder = Path(directory or ARCHIVE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    day = review["closed_uk"][:10]
    path = folder / f"{day}.jsonl"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    key = review["review_id"]
    for line in existing.splitlines():
        if not line.strip():
            continue
        try:
            if json.loads(line).get("review_id") == key:
                return str(path), False
        except (json.JSONDecodeError, TypeError):
            # Preserve unknown historical lines without discarding evidence.
            pass
    encoded = json.dumps(review, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n")
                   + encoded + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return str(path), True


def short_summary(review):
    peak = review["best_observed_gross_pct_approx"]
    peak_text = f"{peak:+.3f}% gross approx" if peak is not None else "unavailable"
    net = review["net_usdt"]
    net_text = f"{net:+.5f} USDT" if net is not None else "unavailable"
    changed = " | PROFIT->LOSS" if review["had_gross_profit_before_net_loss"] else ""
    return (f"{review['side']} {review['instrument']} | net {net_text} | "
            f"peak {peak_text} | reason {review['close_reason_recorded']}"
            f"{changed} | journal {review['closed_uk'][:10]}")


def sync_daily_reviews(state, day=None):
    """Recreate today's missing journal after runner restart or a failed git push.

    Uses ONLY verified historical closes already stored in LIVE state.
    Never queries BloFin, never changes order execution.
    """
    if day is None:
        day = datetime.now(UK).date().isoformat()
    written = 0
    updated_state = 0
    for trade in state.get("trade_history", []):
        if not isinstance(trade, dict):
            continue
        closed = int(trade.get("closed_ms") or 0)
        if closed <= 0:
            continue
        closed_day = datetime.fromtimestamp(
            closed / 1000, timezone.utc).astimezone(UK).date().isoformat()
        if closed_day != day:
            continue
        review = trade.get("post_trade_review")
        if not review or review.get("version") != 1:
            review = make_review(trade)
            trade["post_trade_review"] = review
            updated_state += 1
        _, appended = archive_review(review)
        if appended:
            written += 1
    return {"day": day, "new_journal_rows": written,
            "updated_state_reviews": updated_state}
