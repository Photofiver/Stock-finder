"""One-time, narrowly scoped cancellation of the orphan ZK-USDT OCO order.

This script can never close a futures position, place a new order, or cancel
another instrument. It refuses to remove TP/SL protection if ZK-USDT is open.
"""
from decimal import Decimal
import json

import blofin_live_hourly as bot

INSTRUMENT = "ZK-USDT"
TPSL_ID = "10028562149"
EXPECTED_TP = Decimal("0.013628")
EXPECTED_SL = Decimal("0.013358")


def matches_target(row):
    if not isinstance(row, dict):
        return False
    try:
        return (
            str(row.get("instId")) == INSTRUMENT
            and str(row.get("tpslId")) == TPSL_ID
            and str(row.get("side") or "").lower() == "sell"
            and str(row.get("reduceOnly") or "").lower() == "true"
            and Decimal(str(row.get("tpTriggerPrice"))) == EXPECTED_TP
            and Decimal(str(row.get("slTriggerPrice"))) == EXPECTED_SL
        )
    except (ValueError, ArithmeticError, TypeError):
        return False


def pending_target():
    rows = bot.private_request(
        "GET", "/api/v1/trade/orders-tpsl-pending",
        params={"instId": INSTRUMENT, "tpslId": TPSL_ID},
    )
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected response to pending TP/SL query; no action")
    matches = [r for r in rows if str(r.get("tpslId")) == TPSL_ID]
    if len(matches) > 1:
        raise RuntimeError("Duplicate target ID in pending response; no action")
    return matches[0] if matches else None


def detail_status():
    try:
        row = bot.private_request(
            "GET", "/api/v1/trade/order-tpsl-detail",
            params={"instId": INSTRUMENT, "tpslId": TPSL_ID},
        )
        if isinstance(row, list):
            row = row[0] if row else {}
        return {
            "found": isinstance(row, dict) and str(row.get("tpslId")) == TPSL_ID,
            "state": row.get("state") if isinstance(row, dict) else None,
        }
    except Exception as exc:
        return {
            "found": False,
            "lookup_error": f"{type(exc).__name__}: {exc}",
        }


def run():
    positions = bot.get_open_positions()
    active_zk = [
        {
            "instId": str(p.get("instId")),
            "positionSide": p.get("positionSide"),
        }
        for p in positions if str(p.get("instId")) == INSTRUMENT
    ]
    if active_zk:
        print(json.dumps({
            "result": "ABORT_ZK_POSITION_OPEN",
            "target": TPSL_ID,
            "reason": "Cannot remove active ZK-USDT position protection",
            "active_zk": active_zk,
        }))
        return

    pending = pending_target()
    if pending is None:
        print(json.dumps({
            "result": "ALREADY_NOT_PENDING",
            "target": TPSL_ID,
            "detail": detail_status(),
            "message": "No active order to cancel; app may display stale OCO",
        }))
        return
    if not matches_target(pending):
        print(json.dumps({
            "result": "ABORT_ORDER_MISMATCH",
            "target": TPSL_ID,
            "observed": {
                k: pending.get(k) for k in (
                    "tpslId", "instId", "side", "reduceOnly",
                    "tpTriggerPrice", "slTriggerPrice", "state",
                )
            },
        }))
        return

    # Recheck that a new ZK position has not appeared since first read.
    if any(str(row.get("instId")) == INSTRUMENT
           for row in bot.get_open_positions()):
        print(json.dumps({
            "result": "ABORT_ZK_POSITION_OPEN_BEFORE_CANCEL",
            "target": TPSL_ID,
        }))
        return

    print(json.dumps({
        "result": "ATTEMPT_CANCEL_EXACT_ZK_OCO",
        "target": TPSL_ID,
        "tp": str(EXPECTED_TP),
        "sl": str(EXPECTED_SL),
    }), flush=True)
    try:
        data = bot.private_request(
            "POST", "/api/v1/trade/cancel-tpsl",
            body=[{
                "instId": INSTRUMENT,
                "tpslId": TPSL_ID,
                "clientOrderId": "",
            }],
        )
        rows = data if isinstance(data, list) else [data]
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError("Unexpected cancellation response")
        result = rows[0]
        code = str(result.get("code"))
        message = str(result.get("msg") or "")
    except Exception as exc:
        code = "REQUEST_ERROR"
        message = f"{type(exc).__name__}: {exc}"

    still_pending = pending_target()
    result_label = (
        "CANCELED_CONFIRMED" if still_pending is None and code == "0"
        else "NOT_PENDING_AFTER_ATTEMPT" if still_pending is None
        else "CANCEL_FAILED_ORDER_STILL_ACTIVE"
    )
    print(json.dumps({
        "result": result_label,
        "target": TPSL_ID,
        "cancel_code": code,
        "cancel_message": message,
        "still_pending": still_pending is not None,
        "detail": detail_status(),
    }), flush=True)


if __name__ == "__main__":
    run()
