"""Persist 20-level BloFin order-book snapshots and confirm LIVE entry direction.

The top-of-book BID/ASK alone is not an order-book imbalance indicator.
Depth size is BloFin contract quantity (not executed buying/selling pressure).
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os

DEPTH_LEVELS = 20
MIN_LEVELS_PER_SIDE = 5
MAX_AGE_MS = 15000
MIN_DIRECTIONAL_SHARE_PCT = Decimal(os.getenv("LIVE_OB_MIN_DIRECTIONAL_PCT", "62"))
AUDIT_DIR = os.getenv("LIVE_ORDER_BOOK_AUDIT_DIR", "blofin_order_books")


def _levels(rows, side):
    if not isinstance(rows, list) or len(rows) < MIN_LEVELS_PER_SIDE:
        raise RuntimeError(f"ORDER_BOOK_{side}_DEPTH_UNAVAILABLE")
    result = []
    for row in rows[:DEPTH_LEVELS]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise RuntimeError(f"ORDER_BOOK_{side}_INVALID_LEVEL")
        try:
            price, size = Decimal(str(row[0])), Decimal(str(row[1]))
        except Exception as exc:
            raise RuntimeError(f"ORDER_BOOK_{side}_INVALID_NUMERIC_LEVEL") from exc
        if not (price.is_finite() and size.is_finite() and price > 0 and size >= 0):
            raise RuntimeError(f"ORDER_BOOK_{side}_INVALID_PRICE_OR_SIZE")
        result.append([str(price), str(size), *[str(v) for v in row[2:3]]])
    if not any(Decimal(row[1]) > 0 for row in result):
        raise RuntimeError(f"ORDER_BOOK_{side}_NO_VOLUME")
    return result


def capture(bot, inst, side, rank, signal_close_ms):
    """Fetch a fresh 20-level book just before order submission."""
    if side not in ("LONG", "SHORT"):
        raise ValueError("ORDER_BOOK_INVALID_SIDE")
    rows = bot.market_get(
        "/api/v1/market/books", {"instId": inst, "size": str(DEPTH_LEVELS)}
    )
    data = rows[0] if isinstance(rows, list) and rows else {}
    timestamp_ms = int(data.get("ts") or 0)
    age_ms = bot.now_ms() - timestamp_ms
    if timestamp_ms <= 0 or not (-5000 <= age_ms <= MAX_AGE_MS):
        raise RuntimeError(f"ORDER_BOOK_STALE: {age_ms}ms")
    bids = _levels(data.get("bids"), "BID")
    asks = _levels(data.get("asks"), "ASK")
    if Decimal(bids[0][0]) > Decimal(asks[0][0]):
        raise RuntimeError("ORDER_BOOK_CROSSED_BID_ASK")
    buy_depth = sum((Decimal(p) * Decimal(q) for p, q, *_ in bids), Decimal("0"))
    sell_depth = sum((Decimal(p) * Decimal(q) for p, q, *_ in asks), Decimal("0"))
    total = buy_depth + sell_depth
    if total <= 0:
        raise RuntimeError("ORDER_BOOK_NO_TOTAL_VOLUME")
    buy_pct = buy_depth * 100 / total
    sell_pct = sell_depth * 100 / total
    aligned = buy_pct if side == "LONG" else sell_pct
    return {
        "version": 1,
        "inst": inst, "side": side, "rank": int(rank),
        "signal_close_ms": int(signal_close_ms),
        "captured_ms": int(bot.now_ms()), "book_timestamp_ms": timestamp_ms,
        "age_ms": int(age_ms), "depth_levels_requested": DEPTH_LEVELS,
        "bid_levels": bids, "ask_levels": asks,
        "bid_levels_count": len(bids), "ask_levels_count": len(asks),
        "bid_quote_depth": float(buy_depth), "ask_quote_depth": float(sell_depth),
        "bid_pct": float(buy_pct), "ask_pct": float(sell_pct),
        "directional_pct": float(aligned),
        "min_directional_pct": float(MIN_DIRECTIONAL_SHARE_PCT),
        "direction_confirmed": bool(aligned >= MIN_DIRECTIONAL_SHARE_PCT),
        "indicator": "TOP20_ORDER_BOOK_DEPTH_IMBALANCE",
        "note": "Visible resting depth; can be cancelled or spoofed; not executed market flow",
    }


def append_audit(snapshot, decision, error=None):
    """Write a dated JSONL event on every final pre-order candidate.

    Keeps full 20-level volumes for both accepted and rejected candidates;
    historical trade_history separately retains the passing snapshot.
    An audit-write failure must not silently affect market risk decisions.
    """
    when = snapshot.get("captured_ms") or snapshot.get("signal_close_ms") or 0
    day = datetime.fromtimestamp(when / 1000, timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(AUDIT_DIR, exist_ok=True)
    path = os.path.join(AUDIT_DIR, f"{day}.jsonl")
    event = {"decision": decision, "error": str(error) if error else None, **snapshot}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path
