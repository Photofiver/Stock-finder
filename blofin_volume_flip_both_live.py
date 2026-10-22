"""LIVE volume flip on CLOSED 5m BloFin candles.

Active pivot mode: LONG and SHORT second-boundary entries.
The existing LONG pre-order TP gap <= 0.60% and SL gap >= 0.10%
filter remains; original volume and protection safety checks remain.

LONG: qualified GREEN after qualified RED, green volume > prior red.
SHORT: qualified RED after qualified GREEN, red volume > prior green.
Qualified means each candle volume <= its own prior 20-bar mean.
Exit on any CLOSED opposite-color candle volume > the OPENING signal
candle volume, WITHOUT a volume-average filter; next 5m scan.

No BTC/price/DI/3-4x filters. For NEW entries, broker TP and SL are
the two straight volume-tunnel price lines fixed at the entry signal.
Existing positions retain their previously confirmed TP/SL policy.
Existing legacy red-volume LONG positions retain their original exit.
1x isolated, up to four different coins, shared bankroll and risk gate.
"""
import math
import os
import uuid
from blofin_pivot_tunnel import evaluate_tunnel, pivot_exit_transition, second_boundary_bounce
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

STRATEGY = "VOLUME_FLIP_BOTH_5M_V1"
PIVOT_ENABLED = os.getenv("LIVE_PIVOT_TUNNEL_ENABLED", "false").strip().lower() == "true"
LEGACY_STRATEGY = "REV_SHORT_20W_LONG_VOLUME_V1"
HOLD_POLICY = "VOLUME_FLIP_BOTH_NO_TPSL"
TUNNEL_TPSL_MODE = "TUNNEL_LINES_AT_ENTRY"
SECOND_BOUNCE_SL_MODE = "SECOND_BOUNCE_SL_ONLY"
EMERGENCY_TPSL_MODE = "EMERGENCY_FIXED_AFTER_FILL"
TP_PCT = Decimal("0.0055")
SL_PCT = Decimal("0.005")
TP_POLICY = "TP055"
SL_POLICY = "SL05"
TRIGGER_TYPE = "last"  # Tunnel candles use last traded price, not mark-price PnL.
LEGACY_HOLD_POLICY = "VOLUME_REV_SHORT_LONG"
FIVE = 300_000
VOL_MIN = 3.0
VOL_MAX_EXCLUSIVE = 4.0
BTC15_MAX = 0.062055813
COIN15_MIN = -0.10322961
MAX_POSITIONS = 4  # additional to per-coin uniqueness; existing account cap
MAX_PER_COIN_USDT = Decimal("5")
MIN_PER_COIN_USDT = Decimal("5")
CASH_BUFFER_USDT = Decimal("0.50")
AGE_MAX_MS = 120_000
def _protection_prices(bot, pos, meta):
    """Tick-align broker protection; LONG pivot SL can tighten after closed 5m bars.

    Existing positions continue using their original fixed TP/SL.
    Rounding moves TUNNEL targets to the first tradable tick on or just
    beyond the selected boundary, without inventing a price threshold.
    """
    entry=bot.d(pos.get("reference_entry") or "0")
    tick=bot.d((meta or {}).get("tickSize") or "0")
    if entry<=0 or tick<=0 or not entry.is_finite() or not tick.is_finite():
        raise RuntimeError("Verified entry and tickSize are required for TP/SL")
    if pos.get("tp_sl_mode") == TUNNEL_TPSL_MODE:
        tp_line = bot.d(pos.get("tunnel_tp_line") or "0")
        sl_line = bot.d(pos.get("tunnel_sl_line") or "0")
        if not all(x.is_finite() and x > 0 for x in (tp_line, sl_line)):
            raise RuntimeError("Tunnel TP/SL boundaries missing or invalid")
        if pos["side"] == "LONG":
            tp = (tp_line / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            sl = (sl_line / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            valid = (entry < tp and 0 < sl < tp) if pos.get("sl_trailing_active") else sl < entry < tp
        elif pos["side"] == "SHORT":
            tp = (tp_line / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            sl = (sl_line / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            valid = (0 < tp < sl) if pos.get("sl_trailing_active") else tp < entry < sl
        else:
            raise RuntimeError("Unknown side")
        if not valid:
            raise RuntimeError("Tunnel TP/SL not on the correct side of entry")
        return tp, sl
    if pos.get("tp_sl_mode") == SECOND_BOUNCE_SL_MODE:
        sl_line = bot.d(pos.get("tunnel_sl_line") or "0")
        if pos.get("side") not in ("LONG", "SHORT") or not sl_line.is_finite() or sl_line <= 0:
            raise RuntimeError("Second bounce LONG requires a verified SL")
        sl = (sl_line/tick).to_integral_value(rounding=(ROUND_FLOOR if pos.get("side") == "LONG" else ROUND_CEILING))*tick
        if not (sl > 0 and (pos.get("sl_trailing_active") or (sl < entry if pos.get("side") == "LONG" else sl > entry))):
            raise RuntimeError("Second bounce initial SL must be below entry")
        return Decimal("0"), sl
    if pos["side"]=="LONG":
        tp=((entry*(1+TP_PCT)/tick).to_integral_value(rounding=ROUND_CEILING))*tick
        sl=((entry*(1-SL_PCT)/tick).to_integral_value(rounding=ROUND_CEILING))*tick
        valid=sl<entry<tp
    elif pos["side"]=="SHORT":
        tp=((entry*(1-TP_PCT)/tick).to_integral_value(rounding=ROUND_FLOOR))*tick
        sl=((entry*(1+SL_PCT)/tick).to_integral_value(rounding=ROUND_FLOOR))*tick
        valid=tp<entry<sl
    else:
        raise RuntimeError("Unknown side")
    if not valid:
        raise RuntimeError("tickSize too large for 0.55/0.50 TP/SL")
    return tp,sl



def _ensure_mark_trigger(bot, state, pos, tp, sl):
    """Verify OCO and amend old MARK tunnel triggers to candle-aligned LAST."""
    inst=pos["inst"]
    desired = (TRIGGER_TYPE if pos.get("tp_sl_mode") in (TUNNEL_TPSL_MODE, SECOND_BOUNCE_SL_MODE)
               else "mark")  # Preserve legacy / non-tunnel protections.
    try:
        detail=bot.private_request("GET","/api/v1/trade/order-tpsl-detail",
                    params={"instId":inst,"tpslId":str(pos["tpsl_id"])}) or {}
        if isinstance(detail,list):
            detail=detail[0] if detail else {}
        if (str(detail.get("state") or "").lower()!="live"
            or str(detail.get("tpslId") or "")!=str(pos["tpsl_id"])
            or bot.d(detail.get("tpTriggerPrice") or "0")!=tp
            or bot.d(detail.get("slTriggerPrice") or "0")!=sl):
            raise RuntimeError("Active broker TP/SL is missing or differs from tracked levels")
        types=(str(detail.get("tpTriggerPriceType") or "").lower(),
               str(detail.get("slTriggerPriceType") or "").lower())
        if types==(desired,desired) or (pos.get("tp_sl_mode")==SECOND_BOUNCE_SL_MODE and types[1]==desired):
            pos.update(tp_trigger_type=desired,sl_trigger_type=desired,
                       protection_status=("TUNNEL_LINES_LAST_ACTIVE"
                                          if pos.get("tp_sl_mode") == TUNNEL_TPSL_MODE
                                          else "TP055_SL05_MARK_ACTIVE"))
            pos.pop("trigger_migration_deferred",None)
            pos.pop("mark_amend_request_id",None)
            pos.pop("protection_error",None)
            bot.save_state(state)
            return True
        if types not in (("last","last"),("mark","mark"),("","last"),("","mark")):
            raise RuntimeError("Unexpected broker TP/SL trigger types: "+str(types))
        if pos.get("mark_amend_request_id"):
            raise RuntimeError("Prior mark-trigger amendment is awaiting broker confirmation")
        request_id=("volmark"+uuid.uuid4().hex)[:32]
        pos["mark_amend_request_id"]=request_id
        pos["protection_status"]="MARK_TPSL_AMEND_SENT"
        bot.save_state(state)
        result=bot.private_request("POST","/api/v1/trade/amend-tpsl",body={
            "instId":inst,"tpslId":str(pos["tpsl_id"]),"requestId":request_id,
            "newTpTriggerPriceType":desired,"newSlTriggerPriceType":desired
        })
        row=result[0] if isinstance(result,list) and result else (result or {})
        if str(row.get("code","0"))!="0":
            raise RuntimeError("Broker rejected trigger amendment: "+str(row)[:180])
        # Broker acknowledgement alone is insufficient; verify on next read.
        return _ensure_mark_trigger(bot,state,pos,tp,sl)
    except Exception as exc:
        pos["protection_status"]="TPSL_TRIGGER_VERIFICATION_PENDING"
        pos["protection_error"]=f"{type(exc).__name__}: {exc}"[:250]
        # If MARK remains live at exactly the original tunnel boundaries,
        # do not emergency-liquidate a protected position because an AMEND
        # failed. Mark the migration deferred and pause NEW entries.
        if desired == "last":
            try:
                check=bot.private_request("GET","/api/v1/trade/order-tpsl-detail",
                    params={"instId":inst,"tpslId":str(pos["tpsl_id"])}) or {}
                if isinstance(check,list):
                    check=check[0] if check else {}
                if (str(check.get("state") or "").lower()=="live"
                    and str(check.get("tpslId") or "")==str(pos["tpsl_id"])
                    and bot.d(check.get("tpTriggerPrice") or "0")==tp
                    and bot.d(check.get("slTriggerPrice") or "0")==sl
                    and str(check.get("tpTriggerPriceType") or "").lower()=="mark"
                    and str(check.get("slTriggerPriceType") or "").lower()=="mark"):
                    pos["trigger_migration_deferred"]=True
                    pos["protection_status"]="TUNNEL_MARK_STILL_ACTIVE_AMEND_UNVERIFIED"
                    bot.save_state(state)
                    return True
            except Exception:
                pass
        bot.save_state(state)
        return False


def _ensure_protection(bot,state,pos,meta):
    """One broker-side combined TPSL order; never repeat an uncertain POST."""
    if pos.get("strategy")!=STRATEGY:
        return True
    tp,sl=_protection_prices(bot,pos,meta)
    if pos.get("tpsl_id"):
        return _ensure_mark_trigger(bot,state,pos,tp,sl)
    inst=pos["inst"]
    client=str(pos.get("tpsl_client_order_id") or "")
    if client:
        # The prior POST may have succeeded even if its HTTP response was lost.
        try:
            detail=bot.private_request("GET","/api/v1/trade/order-tpsl-detail",
                                       params={"instId":inst,"clientOrderId":client}) or {}
            if isinstance(detail,list):
                detail=detail[0] if detail else {}
            if (str(detail.get("state") or "").lower()=="live"
                    and detail.get("tpslId")
                    and bot.d(detail.get("tpTriggerPrice") or "0")==tp
                    and bot.d(detail.get("slTriggerPrice") or "0")==sl):
                pos.update(tpsl_id=str(detail["tpslId"]),tp=bot.clean_decimal(tp),
                           sl=bot.clean_decimal(sl),
                           tp_policy=("TUNNEL_LINE" if pos.get("tp_sl_mode") == TUNNEL_TPSL_MODE else TP_POLICY),
                           sl_policy=("TUNNEL_LINE" if pos.get("tp_sl_mode") == TUNNEL_TPSL_MODE else SL_POLICY))
                bot.save_state(state)
                return _ensure_mark_trigger(bot,state,pos,tp,sl)
        except Exception as exc:
            pos["protection_error"]=str(exc)[:250]
        pos["protection_status"]="TPSL_UNCERTAIN_NEEDS_BROKER_CONFIRMATION"
        bot.save_state(state)
        return False
    client=("voltp055"+uuid.uuid4().hex)[:32]
    pos.update(tpsl_client_order_id=client,protection_status="TPSL_POST_STARTED",
               tp=bot.clean_decimal(tp),sl=bot.clean_decimal(sl),
               tp_policy=("TUNNEL_LINE" if pos.get("tp_sl_mode") == TUNNEL_TPSL_MODE else TP_POLICY),
               sl_policy=("TUNNEL_LINE" if pos.get("tp_sl_mode") == TUNNEL_TPSL_MODE else SL_POLICY))
    bot.save_state(state)
    try:
        request_body={
            "instId":inst,"marginMode":bot.MARGIN_MODE,"positionSide":"net",
            "side":"sell" if pos["side"]=="LONG" else "buy",
            "tpTriggerPrice":bot.clean_decimal(tp),"tpOrderPrice":"-1",
            "tpTriggerPriceType":(TRIGGER_TYPE if pos.get("tp_sl_mode") in (TUNNEL_TPSL_MODE, SECOND_BOUNCE_SL_MODE) else "mark"),
            "slTriggerPrice":bot.clean_decimal(sl),
            "slOrderPrice":"-1",
            "slTriggerPriceType":(TRIGGER_TYPE if pos.get("tp_sl_mode") in (TUNNEL_TPSL_MODE, SECOND_BOUNCE_SL_MODE) else "mark"),
            "size":"-1","reduceOnly":"true","clientOrderId":client,
        }
        if pos.get("tp_sl_mode") == SECOND_BOUNCE_SL_MODE:
            for key in ("tpTriggerPrice","tpOrderPrice","tpTriggerPriceType"):
                request_body.pop(key,None)
        response=bot.private_request("POST","/api/v1/trade/order-tpsl",body=request_body)
        row=response[0] if isinstance(response,list) and response else (response or {})
        if str(row.get("code","0"))!="0" or not row.get("tpslId"):
            raise RuntimeError("BloFin TPSL not confirmed: "+str(row)[:200])
    except Exception as exc:
        pos["protection_status"]="TPSL_UNCERTAIN_NEEDS_BROKER_CONFIRMATION"
        pos["protection_error"]=str(exc)[:250]
        bot.save_state(state)
        return False
    pos["tpsl_id"]=str(row["tpslId"])
    bot.save_state(state)
    return _ensure_mark_trigger(bot,state,pos,tp,sl)



def _reconcile_pending_tunnel_sl(bot, state, pos):
    """Adopt an SL amendment only after the LIVE broker order confirms it.

    If the broker still shows the old SL, leave that known protection in place.
    The same requestId is safe to retry; never infer success from POST alone.
    """
    pending = pos.get("tunnel_sl_pending")
    if not pending:
        return True
    detail = bot.private_request("GET", "/api/v1/trade/order-tpsl-detail",
                                 params={"instId":pos["inst"], "tpslId":str(pos["tpsl_id"])}) or {}
    if isinstance(detail, list):
        detail = detail[0] if detail else {}
    if (str(detail.get("state") or "").lower() != "live"
            or str(detail.get("tpslId") or "") != str(pos["tpsl_id"])):
        raise RuntimeError("Pending trailing SL: broker protection not LIVE")
    observed = bot.d(detail.get("slTriggerPrice") or "0")
    prior = bot.d(pending["old_sl"])
    proposed = bot.d(pending["target_sl"])
    if observed == proposed:
        pos["sl"] = bot.clean_decimal(proposed)
        pos["tunnel_sl_line"] = bot.clean_decimal(proposed)
        pos["sl_trailing_active"] = True
        pos["last_sl_trail_close_ms"] = int(pending["close_ms"])
        pos["last_sl_trail_status"] = "BROKER_VERIFIED"
        pos.pop("tunnel_sl_pending", None)
        pos.pop("sl_trail_error", None)
        bot.save_state(state)
        return True
    if observed == prior:
        pos["last_sl_trail_status"] = "PENDING_OLD_SL_STILL_LIVE"
        bot.save_state(state)
        return False
    raise RuntimeError("Pending trailing SL: unexpected broker SL; do not overwrite")



def _confirmed_entry_lower_anchors(bot, state, pos):
    """Recover the two original confirmed lower anchors for older LIVE entries.

    Historical positions saved only upper_anchors under pivot_tunnel.
    Their exact lower anchors remain in the immutable entry signal snapshot.
    Do not infer pivots from newer candles: verify timestamp, last pivot and
    direction before adopting the stored entry-time geometry.
    """
    tunnel = pos.get("pivot_tunnel") or {}
    anchors = tunnel.get("lower_anchors") or []
    if len(anchors) == 2:
        return anchors
    snap = (pos.get("signal_snapshot") or {}).get("volume_price_tunnel") or {}
    saved = snap.get("lower_anchors") or []
    last = tunnel.get("last_confirmed_low") or {}
    try:
        if (snap.get("status") != "VALID"
                or snap.get("tunnel_direction") != pos.get("side")
                or int(snap.get("signal_close_ms") or 0) != int(pos.get("signal_close_ms") or 0)
                or len(saved) != 2
                or int(saved[0]["open_ms"]) >= int(saved[1]["open_ms"])
                or int(saved[1]["open_ms"]) != int(last["open_ms"])
                or bot.d(saved[1]["price"]) != bot.d(last["price"])
                or (int(saved[1]["confirmed_close_ms"]) != int(pos["signal_close_ms"])
                    if pos.get("side") == "LONG"
                    else int(saved[1]["confirmed_close_ms"]) > int(pos["signal_close_ms"]))
                or any(bot.d(a["price"]) <= 0 or
                       int(a["confirmed_close_ms"]) > int(pos["signal_close_ms"])
                       for a in saved)):
            raise ValueError("entry anchor snapshot failed validation")
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        pos["sl_trail_error"] = f"MISSING_CONFIRMED_ENTRY_LOWER_ANCHORS: {exc}"[:220]
        pos["last_sl_trail_status"] = "BLOCK_MISSING_LOWER_ANCHORS"
        bot.save_state(state)
        return []
    tunnel["lower_anchors"] = [dict(saved[0]), dict(saved[1])]
    pos["pivot_tunnel"] = tunnel
    pos["last_sl_trail_status"] = "RECOVERED_ENTRY_LOWER_ANCHORS"
    bot.save_state(state)
    return tunnel["lower_anchors"]


def _confirmed_entry_upper_anchors(bot, state, pos):
    """Recover original confirmed resistance anchors, never guess newer pivots."""
    tunnel = pos.get("pivot_tunnel") or {}
    anchors = tunnel.get("upper_anchors") or []
    if len(anchors) == 2:
        return anchors
    snap = (pos.get("signal_snapshot") or {}).get("volume_price_tunnel") or {}
    saved = snap.get("upper_anchors") or []
    last = tunnel.get("last_confirmed_high") or {}
    try:
        if (snap.get("status") != "VALID"
                or snap.get("tunnel_direction") != pos.get("side")
                or int(snap.get("signal_close_ms") or 0) != int(pos.get("signal_close_ms") or 0)
                or len(saved) != 2
                or int(saved[0]["open_ms"]) >= int(saved[1]["open_ms"])
                or int(saved[1]["open_ms"]) != int(last["open_ms"])
                or bot.d(saved[1]["price"]) != bot.d(last["price"])
                or (int(saved[1]["confirmed_close_ms"]) != int(pos["signal_close_ms"])
                    if pos.get("side") == "SHORT"
                    else int(saved[1]["confirmed_close_ms"]) > int(pos["signal_close_ms"]))
                or any(bot.d(a["price"]) <= 0 or
                       int(a["confirmed_close_ms"]) > int(pos["signal_close_ms"])
                       for a in saved)):
            raise ValueError("entry upper anchor snapshot failed validation")
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        pos["sl_trail_error"] = f"MISSING_CONFIRMED_ENTRY_UPPER_ANCHORS: {exc}"[:220]
        bot.save_state(state)
        return []
    tunnel["upper_anchors"] = [dict(saved[0]), dict(saved[1])]
    pos["pivot_tunnel"] = tunnel
    bot.save_state(state)
    return tunnel["upper_anchors"]


def _trail_confirmed_pivot_long_sl(bot, state, pos, meta, rows, close_ms, ticker):
    """Tighten LONG support or SHORT resistance SL, never loosen.

    Only confirmed 5m candles and already-confirmed entry pivot anchors.
    Amend the existing tpslId in place; never cancel the prior protective order.
    """
    if (pos.get("side") not in ("LONG", "SHORT") or not pos.get("pivot_tunnel")
            or pos.get("tp_sl_mode") not in (SECOND_BOUNCE_SL_MODE, TUNNEL_TPSL_MODE)
            or pos.get("exit_pending") or not pos.get("tpsl_id")
            or pos.get("tunnel_tp_pending")):
        return None
    if close_ms <= int(pos.get("signal_close_ms") or 0):
        return None
    candle = rows.get(close_ms-FIVE)
    if not candle:
        return None
    anchors = (_confirmed_entry_lower_anchors(bot, state, pos) if pos["side"] == "LONG"
               else _confirmed_entry_upper_anchors(bot, state, pos))
    if len(anchors) != 2:
        return {"status":"BLOCK_MISSING_ENTRY_SL_ANCHORS"}
    if not meta:
        pos["last_sl_trail_status"] = "SKIP_MISSING_INSTRUMENT_META"
        return None
    try:
        left, right = anchors
        t1, t2 = int(left["open_ms"]), int(right["open_ms"])
        if not (t1 < t2 and close_ms >= int(right["confirmed_close_ms"])):
            return None
        v1, v2 = bot.d(left["price"]), bot.d(right["price"])
        tick = bot.d(meta.get("tickSize") or "0")
        if not (tick > 0 and v1 > 0 and v2 > 0):
            return None
        # Use original confirmed lower support (LONG) or upper resistance (SHORT).
        raw = v1 + (v2-v1) * bot.d(close_ms-t1) / bot.d(t2-t1)
        target = (raw/tick).to_integral_value(
            rounding=(ROUND_FLOOR if pos["side"] == "LONG" else ROUND_CEILING))*tick
        previous = bot.d(pos.get("sl") or "0")
        improves = target > previous if pos["side"] == "LONG" else target < previous
        if not (improves and previous > 0):
            pos["last_sl_trail_status"] = "NO_RISK_REDUCING_SL_TICK"
            return None
        price_close = bot.d(candle[3])
        price_live = bot.d((ticker or {}).get("last") or "0")
        # Keep LONG SL below price and SHORT SL above price to avoid immediate execution.
        invalid_market = (target >= min(price_close, price_live) if pos["side"] == "LONG"
                          else target <= max(price_close, price_live))
        if price_live <= 0 or invalid_market:
            pos["last_sl_trail_status"] = "SKIP_SL_ON_WRONG_SIDE_OF_MARKET"
            return None
        tp = bot.d(pos.get("tp") or "0")
        if tp > 0 and (target >= tp if pos["side"] == "LONG" else target <= tp):
            pos["last_sl_trail_status"] = "SKIP_SL_CROSSES_TP"
            return None
    except (KeyError, TypeError, ValueError, ArithmeticError, ZeroDivisionError) as exc:
        pos["sl_trail_error"] = f"INVALID_TUNNEL_SL: {exc}"[:180]
        return None

    pending = pos.get("tunnel_sl_pending")
    if pending:
        # An ambiguous POST may have succeeded: read broker state before
        # attempting another request or changing the tracked SL.
        try:
            if _reconcile_pending_tunnel_sl(bot, state, pos):
                return {"status":"CONFIRMED_PREVIOUS", "sl":pos["sl"]}
            # This specific API validation error means the original request
            # was REJECTED (not ambiguous). If the live broker still has our
            # previous SL, clear that rejected idempotency key and prepare a
            # complete new request. Never replace uncertain requests.
            previous_error = str(pos.get("sl_trail_error") or "")
            if ("152001" in previous_error
                    and "newSlTriggerPriceType" in previous_error
                    and pos.get("last_sl_trail_status") == "PENDING_OLD_SL_STILL_LIVE"):
                pos.pop("tunnel_sl_pending", None)
                pos.pop("sl_trail_error", None)
                pos["last_sl_trail_status"] = "RETRY_BROKER_REJECTED_152001"
                bot.save_state(state)
                pending = None
            else:
                request = pending
        except Exception as exc:
            pos["sl_trail_error"] = f"RECONCILE_FAILED: {exc}"[:200]
            bot.save_state(state)
            return {"status":"PENDING_VERIFICATION"}
    if not pending:
        # Confirm currently live protection BEFORE requesting a higher stop.
        try:
            detail = bot.private_request("GET","/api/v1/trade/order-tpsl-detail",
                          params={"instId":pos["inst"],"tpslId":str(pos["tpsl_id"])}) or {}
            if isinstance(detail,list):
                detail=detail[0] if detail else {}
            if (str(detail.get("state") or "").lower()!="live"
                    or str(detail.get("tpslId") or "")!=str(pos["tpsl_id"])
                    or bot.d(detail.get("slTriggerPrice") or "0")!=previous):
                raise RuntimeError("Old protective SL differs from LIVE broker")
        except Exception as exc:
            pos["sl_trail_error"] = f"BROKER_READ_FAILED: {exc}"[:200]
            bot.save_state(state)
            return {"status":"BROKER_READ_FAILED"}
        request = {"old_sl":bot.clean_decimal(previous),
                   "target_sl":bot.clean_decimal(target),
                   "close_ms":int(close_ms),
                   "request_id":("voltrail"+uuid.uuid4().hex)[:32]}
        # Save BEFORE POST so a timeout cannot lead to a second new request.
        pos["tunnel_sl_pending"] = request
        bot.save_state(state)
    try:
        result=bot.private_request("POST","/api/v1/trade/amend-tpsl",body={
            "instId":pos["inst"],"tpslId":str(pos["tpsl_id"]),
            "requestId":request["request_id"],
            "newSlTriggerPrice":request["target_sl"],
            # BloFin rejects price-only changes with code 152001 when the
            # trigger type is omitted, even though API docs mark it optional.
            "newSlTriggerPriceType":TRIGGER_TYPE,
            "newSlOrderPrice":"-1",
        })
        row=result[0] if isinstance(result,list) and result else (result or {})
        if str(row.get("code","0"))!="0":
            raise RuntimeError("BloFin rejected trailing SL: "+str(row)[:160])
    except Exception as exc:
        pos["sl_trail_error"] = f"AMEND_UNCERTAIN: {exc}"[:220]
        bot.save_state(state)
        return {"status":"PENDING_VERIFICATION"}
    try:
        confirmed=_reconcile_pending_tunnel_sl(bot,state,pos)
        return ({"status":"BROKER_VERIFIED","sl":pos["sl"]} if confirmed
                else {"status":"PENDING_VERIFICATION"})
    except Exception as exc:
        pos["sl_trail_error"]=f"VERIFY_UNCERTAIN: {exc}"[:220]
        bot.save_state(state)
        return {"status":"PENDING_VERIFICATION"}


def _project_confirmed_tunnel_line(bot, anchors, close_ms, tick, rounding):
    if len(anchors or []) != 2:
        raise ValueError("two confirmed original tunnel anchors required")
    a, b = anchors
    t1, t2 = int(a["open_ms"]), int(b["open_ms"])
    if t1 >= t2 or close_ms < int(b["confirmed_close_ms"]):
        raise ValueError("unconfirmed or reversed tunnel anchors")
    p1, p2 = bot.d(a["price"]), bot.d(b["price"])
    if not all(x.is_finite() and x > 0 for x in (p1, p2, tick)):
        raise ValueError("invalid pivot price/tick")
    value = p1 + (p2 - p1) * bot.d(close_ms - t1) / bot.d(t2 - t1)
    if not value.is_finite() or value <= 0:
        raise ValueError("invalid projected boundary")
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _reconcile_pending_tunnel_tp(bot, state, pos):
    """Only accept a moved TP after reading it from the SAME live exchange OCO."""
    pending = pos.get("tunnel_tp_pending")
    if not pending:
        return True
    detail = bot.private_request("GET", "/api/v1/trade/order-tpsl-detail",
                 params={"instId": pos["inst"], "tpslId": str(pos["tpsl_id"])}) or {}
    if isinstance(detail, list):
        detail = detail[0] if detail else {}
    if (str(detail.get("state") or "").lower() != "live"
            or str(detail.get("tpslId") or "") != str(pos["tpsl_id"])):
        raise RuntimeError("Broker OCO not LIVE")
    if bot.d(detail.get("slTriggerPrice") or "0") != bot.d(pending["old_sl"]):
        raise RuntimeError("Broker stop differs during TP amendment")
    observed = bot.d(detail.get("tpTriggerPrice") or "0")
    if observed == bot.d(pending["new_tp"]):
        pos["tp"] = pending["new_tp"]
        pos["tunnel_tp_line"] = pending["new_tp"]
        pos["last_tp_trail_close_ms"] = int(pending["close_ms"])
        pos["last_tp_trail_status"] = "BROKER_VERIFIED"
        pos.pop("tunnel_tp_pending", None)
        pos.pop("tp_trail_error", None)
        bot.save_state(state)
        return True
    if observed == bot.d(pending["old_tp"]):
        pos["last_tp_trail_status"] = "PENDING_OLD_TP_STILL_LIVE"
        bot.save_state(state)
        return False
    raise RuntimeError("Unexpected broker TP; preserve pending amendment")


def _trail_confirmed_pivot_tp(bot, state, pos, meta, rows, close_ms, ticker):
    """Project TP from entry pivots on each closed 5m candle; second touch is virtual."""
    side = pos.get("side")
    mode = pos.get("tp_sl_mode")
    if (side not in ("LONG", "SHORT")
            or mode not in (SECOND_BOUNCE_SL_MODE, TUNNEL_TPSL_MODE)
            or (mode == SECOND_BOUNCE_SL_MODE and side != "LONG"
                and side != "SHORT")
            or not pos.get("pivot_tunnel") or not pos.get("tpsl_id")
            or pos.get("exit_pending") or pos.get("trigger_migration_deferred")
            or pos.get("tunnel_sl_pending") or not meta
            or close_ms <= int(pos.get("signal_close_ms") or 0)
            or close_ms - FIVE not in rows):
        return None
    try:
        tick = bot.d(meta.get("tickSize") or "0")
        anchors = (_confirmed_entry_upper_anchors(bot, state, pos) if side == "LONG"
                   else _confirmed_entry_lower_anchors(bot, state, pos))
        projected = _project_confirmed_tunnel_line(
            bot, anchors, close_ms, tick,
            ROUND_CEILING if side == "LONG" else ROUND_FLOOR)
        if mode == SECOND_BOUNCE_SL_MODE:
            # Keep the exchange TP ABSENT. second_boundary_bounce() owns exit.
            pos["tunnel_tp_projected"] = bot.clean_decimal(projected)
            pos["last_tp_projection_close_ms"] = int(close_ms)
            bot.save_state(state)
            return {"status": "SECOND_TOUCH_TARGET_TRACKED",
                    "projected_tp": pos["tunnel_tp_projected"]}
        previous = bot.d(pos.get("tp") or "0")
        previous_sl = bot.d(pos.get("sl") or "0")
        close_price = bot.d(rows[close_ms - FIVE][3])
        live_price = bot.d((ticker or {}).get("last") or "0")
        if not all(x.is_finite() and x > 0 for x in
                   (previous, previous_sl, close_price, live_price)):
            raise ValueError("invalid last price or protective price")
        if not pos.get("tunnel_tp_pending"):
            if projected == previous:
                return None
            if side == "LONG" and projected <= max(close_price, live_price, previous_sl):
                return None
            if side == "SHORT" and projected >= min(close_price, live_price, previous_sl):
                return None
    except (KeyError, TypeError, ValueError, ArithmeticError, ZeroDivisionError) as exc:
        pos["tp_trail_error"] = f"INVALID_TUNNEL_TP: {exc}"[:180]
        bot.save_state(state)
        return None
    pending = pos.get("tunnel_tp_pending")
    if pending:
        try:
            if _reconcile_pending_tunnel_tp(bot, state, pos):
                return {"status": "BROKER_VERIFIED_PREVIOUS", "tp": pos["tp"]}
        except Exception as exc:
            pos["tp_trail_error"] = f"PENDING_VERIFY_ERROR: {exc}"[:200]
            bot.save_state(state)
            return {"status": "PENDING_VERIFICATION"}
    else:
        try:
            detail = bot.private_request("GET", "/api/v1/trade/order-tpsl-detail",
                       params={"instId": pos["inst"], "tpslId": str(pos["tpsl_id"])}) or {}
            if isinstance(detail, list):
                detail = detail[0] if detail else {}
            if (str(detail.get("state") or "").lower() != "live"
                    or str(detail.get("tpslId") or "") != str(pos["tpsl_id"])
                    or bot.d(detail.get("tpTriggerPrice") or "0") != previous
                    or bot.d(detail.get("slTriggerPrice") or "0") != previous_sl
                    or str(detail.get("tpTriggerPriceType") or "").lower() != TRIGGER_TYPE
                    or str(detail.get("slTriggerPriceType") or "").lower() != TRIGGER_TYPE):
                raise RuntimeError("Existing live OCO differs from local TP/SL")
        except Exception as exc:
            pos["tp_trail_error"] = f"BROKER_READ_FAILED: {exc}"[:200]
            bot.save_state(state)
            return {"status": "BROKER_READ_FAILED"}
        pending = {"old_tp": bot.clean_decimal(previous),
                   "new_tp": bot.clean_decimal(projected),
                   "old_sl": bot.clean_decimal(previous_sl),
                   "close_ms": int(close_ms),
                   "request_id": ("voltp" + uuid.uuid4().hex)[:32]}
        pos["tunnel_tp_pending"] = pending
        bot.save_state(state)
    try:
        response = bot.private_request("POST", "/api/v1/trade/amend-tpsl", body={
            "instId": pos["inst"], "tpslId": str(pos["tpsl_id"]),
            "requestId": pending["request_id"],
            "newTpTriggerPrice": pending["new_tp"],
            "newTpTriggerPriceType": TRIGGER_TYPE,
            "newTpOrderPrice": "-1",
        })
        row = response[0] if isinstance(response, list) and response else (response or {})
        if str(row.get("code", "0")) != "0":
            raise RuntimeError("BloFin rejected TP amendment: " + str(row)[:180])
    except Exception as exc:
        pos["tp_trail_error"] = f"AMEND_UNCERTAIN: {exc}"[:200]
        bot.save_state(state)
        return {"status": "PENDING_VERIFICATION"}
    try:
        confirmed = _reconcile_pending_tunnel_tp(bot, state, pos)
        return ({"status": "BROKER_VERIFIED", "tp": pos["tp"]}
                if confirmed else {"status": "PENDING_VERIFICATION"})
    except Exception as exc:
        pos["tp_trail_error"] = f"VERIFY_UNCERTAIN: {exc}"[:200]
        bot.save_state(state)
        return {"status": "PENDING_VERIFICATION"}


def _trail_confirmed_pivot_tpsl(bot, state, pos, meta, rows, close_ms, ticker):
    """Shared updater for both trade directions; SL always risk-reducing."""
    updated = {}
    try:
        sl = _trail_confirmed_pivot_long_sl(
            bot, state, pos, meta, rows, close_ms, ticker)
        if sl:
            updated["sl"] = sl
    except Exception as exc:
        pos["sl_trail_error"] = f"{type(exc).__name__}: {exc}"[:220]
        bot.save_state(state)
        updated["sl_error"] = pos["sl_trail_error"]
    try:
        tp = _trail_confirmed_pivot_tp(
            bot, state, pos, meta, rows, close_ms, ticker)
        if tp:
            updated["tp"] = tp
    except Exception as exc:
        pos["tp_trail_error"] = f"{type(exc).__name__}: {exc}"[:220]
        bot.save_state(state)
        updated["tp_error"] = pos["tp_trail_error"]
    return updated or None


def _by_ts(bars, close_ms):
    """Use confirmed closed bars only, excluding any candle not yet closed."""
    valid = {}
    for bar in bars or []:
        ts = int(bar["ts"])
        if ts + FIVE > close_ms:
            continue
        try:
            o, h, l, c, v = (float(bar[k]) for k in ("o", "h", "l", "c", "v"))
        except (TypeError, ValueError, KeyError):
            continue
        if not all(math.isfinite(n) for n in (o, h, l, c, v)):
            continue
        if min(o, h, l, c) <= 0 or v < 0 or h < max(o, c) or l > min(o, c):
            continue
        valid[ts] = (o, h, l, c, v)
    return valid



def _readonly_entry_indicators(bars, signal_close_ms, side, signal_price):
    """Log diagnostics at the confirmed 5m signal close; NEVER gate an order.

    Reuse the bot's existing calculation functions and already-fetched candles.
    No market/API requests. Later candles are excluded to avoid look-ahead.
    """
    result = {
        "indicator_timeframe": "5m",
        "indicator_signal_close_ms": int(signal_close_ms),
        "indicator_status": "UNAVAILABLE",
        "rsi14": None, "rsi14_previous": None,
        "macd_dif": None, "macd_dea": None, "macd_hist": None,
        "macd_hist_previous": None,
        "stoch_k": None, "stoch_d": None,
        "stoch_k_previous": None, "stoch_d_previous": None,
        "sma10": None, "sma20": None,
        "atr14_pct": None, "obv_delta_5": None,
        "sr_levels": None, "sr_entry_decision_readonly": None,
    }
    try:
        # Match prior LIVE JSON indicator definitions without a new dependency.
        import blofin_live_hourly as indicators
        import blofin_support_resistance as sr

        latest_ts = int(signal_close_ms) - FIVE
        confirmed = _by_ts(bars, int(signal_close_ms))
        continuous = []
        for offset in range(200):
            bar = confirmed.get(latest_ts - offset * FIVE)
            if bar is None:
                break
            continuous.append(bar)
        continuous.reverse()
        if not continuous:
            return result
        series = [
            {"o": o, "h": high, "l": low, "c": close, "v": volume}
            for o, high, low, close, volume in continuous
        ]
        rsi = indicators.rsi_wilder(series, 14)
        macd_dif, macd_dea, macd_hist = indicators.macd_series(series)
        stoch_k, stoch_d = indicators.stochastic_kd_series(series, 14, 3)
        n = len(series)
        last = series[-1]

        result.update({
            "rsi14": rsi[-1],
            "rsi14_previous": rsi[-2] if n >= 2 else None,
            "macd_dif": macd_dif[-1],
            "macd_dea": macd_dea[-1],
            "macd_hist": macd_hist[-1],
            "macd_hist_previous": macd_hist[-2] if n >= 2 else None,
            "stoch_k": stoch_k[-1],
            "stoch_d": stoch_d[-1],
            "stoch_k_previous": stoch_k[-2] if n >= 2 else None,
            "stoch_d_previous": stoch_d[-2] if n >= 2 else None,
            "sma10": sum(bar["c"] for bar in series[-10:]) / 10 if n >= 10 else None,
            "sma20": sum(bar["c"] for bar in series[-20:]) / 20 if n >= 20 else None,
        })
        if n >= 15:
            ranges = [
                max(row["h"] - row["l"], abs(row["h"] - series[i-1]["c"]),
                    abs(row["l"] - series[i-1]["c"]))
                for i, row in enumerate(series) if i >= n - 14
            ]
            result["atr14_pct"] = 100.0 * (sum(ranges) / 14) / last["c"]
        if n >= 6:
            result["obv_delta_5"] = sum(
                (1 if series[i]["c"] > series[i-1]["c"]
                 else -1 if series[i]["c"] < series[i-1]["c"] else 0) * series[i]["v"]
                for i in range(n-5, n)
            )
        result["indicator_status"] = (
            "OK" if rsi[-1] is not None and macd_hist[-1] is not None
            and stoch_d[-1] is not None else "INSUFFICIENT_CONFIRMED_HISTORY"
        )

        # Historical support/resistance levels are OBSERVATIONS ONLY.
        # Two subsequent CLOSED bars confirm a swing, never future ones.
        sr_bars = [
            {"ts": latest_ts - (n - 1 - i) * FIVE,
             "h": bar["h"], "l": bar["l"]}
            for i, bar in enumerate(series)
        ]
        try:
            levels = sr.confirmed_levels(sr_bars, signal_close_ms, FIVE)
            result["sr_levels"] = levels
            result["sr_entry_decision_readonly"] = sr.check_entry(
                levels, side, signal_price
            )
        except (ValueError, TypeError, KeyError) as exc:
            result["sr_diagnostic_error"] = f"{type(exc).__name__}: {exc}"[:160]
    except Exception as exc:
        # Data collection must NEVER block or change a qualified LIVE trade.
        result["indicator_status"] = "ERROR"
        result["indicator_error"] = f"{type(exc).__name__}: {exc}"[:160]
    return result


def _previous10_mean(series, ts):
    previous = [series.get(ts - i*FIVE) for i in range(1, 11)]
    return sum(x[4] for x in previous) / 10.0 if all(x is not None for x in previous) else None


def _previous20_mean(series, ts):
    """Average volume of the 20 FULLY CLOSED 5m candles before ts."""
    previous = [series.get(ts - i*FIVE) for i in range(1, 21)]
    return sum(x[4] for x in previous) / 20.0 if all(x is not None for x in previous) else None


def _completed_coin15_pct(series, ts):
    """Must be a FULLY COMPLETED 15m block, never the running 15m candle."""
    block = 3 * FIVE
    last_block_start = ((ts + FIVE)//block - 1)*block
    end = series.get(last_block_start + 2*FIVE)
    before = series.get(last_block_start - FIVE)
    if end is None or before is None:
        return None
    return 100.0 * (end[3]/before[3] - 1.0)


def _qualified(rows, ts):
    candle = rows.get(ts)
    mean = _previous20_mean(rows, ts)
    return candle is not None and mean is not None and mean > 0 and candle[4] <= mean


def entry_signal(bot, bars, expected_close_ms):
    """Two immediately consecutive fully closed opposite-colour 5m candles.
    Both must pass own previous-20-closed-bars volume mean (not future bars).
    """
    if bot.SIGNAL_MINUTES != 5:
        return None
    ts = int(expected_close_ms) - FIVE
    rows = _by_ts(bars, int(expected_close_ms))
    now = rows.get(ts)
    prev = rows.get(ts-FIVE)
    if not now or not prev or not _qualified(rows, ts) or not _qualified(rows, ts-FIVE):
        return None
    if prev[3] < prev[0] and now[3] > now[0] and now[4] > prev[4]:
        side = "LONG"
    elif prev[3] > prev[0] and now[3] < now[0] and now[4] > prev[4]:
        side = "SHORT"
    else:
        return None
    mean20 = _previous20_mean(rows, ts)
    return {
        "side":side,"strategy":STRATEGY,
        "signal_close_ms":int(expected_close_ms),
        "close":now[3],"signal_volume":now[4],
        # Ranking is relative to each coin's OWN prior 20 closed 5m bars.
        # Entry qualification above already guarantees mean20 > 0.
        "volume_strength_20":now[4]/mean20,
        "previous_opposite_volume":prev[4],
        "volume_ratio":now[4]/prev[4] if prev[4] else None,
        "signal_mean20_prev":mean20,
        "previous_mean20_prev":_previous20_mean(rows,ts-FIVE),
        "signal_candle_ts":ts,
        **_readonly_entry_indicators(bars, expected_close_ms, side, now[3]),
    }


LONG_ENTRY_MAX_RANK = 200


def long_entry_extra_rejection(sig, rank):
    """Extra LONG entry gates only; SHORT and existing positions are unchanged.

    Compare two consecutive 20-bar means formed solely from CLOSED 5m
    candles before the entry signal. Never admit missing/invalid values.
    """
    if sig.get("side") != "LONG":
        return None
    if not 1 <= rank <= LONG_ENTRY_MAX_RANK:
        return "LONG_RANK_OUTSIDE_TOP200"
    try:
        current = float(sig["signal_mean20_prev"])
        previous = float(sig["previous_mean20_prev"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return "LONG_VOLUME_MA20_UNAVAILABLE"
    if not all(math.isfinite(value) and value > 0 for value in (current, previous)):
        return "LONG_VOLUME_MA20_UNAVAILABLE"
    if current >= previous:
        return "LONG_VOLUME_MA20_NOT_DECLINING"
    return None


SHORT_ENTRY_MIN_ADVERSE_GAP_PCT = Decimal("0.05")
SHORT_ENTRY_MAX_SPREAD_PCT = Decimal(
    os.getenv("BLOFIN_SHORT_ENTRY_MAX_SPREAD_PCT", "0.05")
)


def short_entry_extra_rejection(side, quote):
    """SHORT-only entry quote gates, with unchanged LONG and exits.

    On SHORT positive adverse_gap_pct means bid below the signal close.
    The quote is checked for freshness and maximum adverse gap upstream.
    """
    if side != "SHORT":
        return None
    try:
        gap = Decimal(str(quote["adverse_gap_pct"]))
        spread = Decimal(str(quote["spread_pct"]))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return "SHORT_QUOTE_METRICS_UNAVAILABLE"
    if not gap.is_finite() or not spread.is_finite() or spread < 0:
        return "SHORT_QUOTE_METRICS_UNAVAILABLE"
    if gap < SHORT_ENTRY_MIN_ADVERSE_GAP_PCT:
        return "SHORT_ADVERSE_GAP_BELOW_005PCT"
    if SHORT_ENTRY_MAX_SPREAD_PCT > 0 and spread > SHORT_ENTRY_MAX_SPREAD_PCT:
        return "SHORT_SPREAD_ABOVE_005PCT"
    return None


UK_TZ = ZoneInfo("Europe/London")


def _volume_trend_midnight_ms(signal_close_ms):
    """UK-local midnight for the latest FULLY CLOSED 5m candle."""
    candle_open_ms = int(signal_close_ms) - FIVE
    local_open = datetime.fromtimestamp(candle_open_ms / 1000, UK_TZ)
    midnight = local_open.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def volume_trend_from_bars(bars, expected_close_ms, previous=None):
    """Carry forward the day's dominant-volume trend, using only closed bars.

    LONG peak updates on GREEN, flips to SHORT only for RED > LONG peak.
    SHORT peak updates on RED, flips to LONG only for GREEN > SHORT peak.
    Exact ties and dojis never reverse the trend. Daily reset: 00:00 UK.

    Returns None when historical candle coverage is incomplete; callers must
    NOT admit new entries using a guessed trend. No additional API requests.
    """
    day_start = _volume_trend_midnight_ms(expected_close_ms)
    rows = _by_ts(bars, expected_close_ms)
    if (expected_close_ms - FIVE) not in rows:
        return None

    valid_prev = (
        isinstance(previous, dict)
        and previous.get("day_start_ms") == day_start
        and previous.get("trend") in ("LONG", "SHORT", "NEUTRAL")
        and isinstance(previous.get("last_close_ms"), int)
        and day_start <= previous["last_close_ms"] <= expected_close_ms
        and isinstance(previous.get("peak_volume"), (int, float))
        and math.isfinite(previous["peak_volume"])
        and previous["peak_volume"] >= 0
    )
    if valid_prev:
        direction = previous["trend"]
        peak = float(previous["peak_volume"])
        first_close_ms = previous["last_close_ms"] + FIVE
        trend_started = previous.get("trend_started_close_ms")
    else:
        direction, peak, trend_started = "NEUTRAL", 0.0, None
        first_close_ms = day_start + FIVE

    if first_close_ms > expected_close_ms:
        return dict(previous) if valid_prev and previous["last_close_ms"] == expected_close_ms else None

    # Every bar since the last trusted checkpoint must be present, in order.
    for candle_close_ms in range(first_close_ms, expected_close_ms + 1, FIVE):
        bar = rows.get(candle_close_ms - FIVE)
        if bar is None:
            return None
        o, _, _, c, vol = bar
        if c == o or vol <= 0:
            continue
        color = "LONG" if c > o else "SHORT"
        if direction == "NEUTRAL":
            direction, peak, trend_started = color, vol, candle_close_ms
        elif color == direction:
            peak = max(peak, vol)
        elif vol > peak:
            direction, peak, trend_started = color, vol, candle_close_ms

    return {
        "day_start_ms": day_start,
        "last_close_ms": int(expected_close_ms),
        "trend": direction,
        "peak_volume": peak,
        "trend_started_close_ms": trend_started,
    }


def _bootstrap_volume_trend_history(bot, inst, expected_close_ms, previous=None):
    """Fetch a complete UK-day history ONLY for an eligible entry signal.

    The regular TOP200 scanner fetches 240 bars (~20h); late in the UK day
    that is insufficient to reconstruct midnight's trend on first deployment.
    BloFin supports up to 1440 candles; 400 closed 5m bars cover even a
    25-hour DST day. Never use unconfirmed/future candles for decisions.
    """
    raw = bot.market_get("/api/v1/market/candles", {
        "instId": inst, "bar": "5m", "limit": "400",
    })
    bars = bot.parse_candles(raw)
    return volume_trend_from_bars(bars, expected_close_ms, previous)


VOLUME_TUNNEL_LOOKBACK_BARS = 20


def volume_price_tunnel_from_bars(bars, expected_close_ms, side):
    """Straight PRICE lines anchored to the two strongest CLOSED volume bars
    of each colour (not the first/last bars or the volume chart's Y scale).

    SHORT reference: RED candle highs above GREEN candle lows.
    LONG reference: GREEN candle highs above RED candle lows.
    At a reversal, the lines exchange order; that is a trend change, NOT
    corrupt data. Never use the signal candle to choose its own anchors.
    """
    timestamp = int(expected_close_ms)
    result = {
        "rule": "STRAIGHT_STRONGEST_VOLUME_PRICE_TUNNEL_20BAR_V2",
        "requested_side": side,
        "timeframe": "5m",
        "lookback_bars": VOLUME_TUNNEL_LOOKBACK_BARS,
        "signal_close_ms": timestamp,
        "status": "UNAVAILABLE",
        "entry_allowed": False,
        "breakout_confirmed": False,
    }
    if side not in ("LONG", "SHORT"):
        result["reason"] = "INVALID_SIDE"
        return result

    rows = _by_ts(bars, timestamp)
    signal = rows.get(timestamp - FIVE)
    previous = rows.get(timestamp - 2 * FIVE)
    if signal is None or previous is None:
        result["reason"] = "MISSING_CLOSED_SIGNAL"
        return result

    colours = {"GREEN": [], "RED": []}
    for age in range(VOLUME_TUNNEL_LOOKBACK_BARS + 1, 1, -1):
        candle = rows.get(timestamp - age * FIVE)
        if candle is None:
            result["reason"] = "MISSING_CHANNEL_HISTORY"
            return result
        o, high, low, close, volume = candle
        if volume <= 0 or close == o:
            continue
        colour = "GREEN" if close > o else "RED"
        colours[colour].append({
            "close_ms": timestamp - (age - 1) * FIVE,
            "volume": volume,
            "high": high, "low": low, "close": close,
        })

    if len(colours["GREEN"]) < 2 or len(colours["RED"]) < 2:
        result["reason"] = "INSUFFICIENT_GREEN_RED_ANCHORS"
        return result

    # Select by volume first, THEN order the two points chronologically.
    # Equal volumes are deterministic; no future candle or smoothed line.
    strongest = {
        colour: sorted(
            sorted(colours[colour], key=lambda bar: (-bar["volume"], bar["close_ms"]))[:2],
            key=lambda bar: bar["close_ms"],
        )
        for colour in ("GREEN", "RED")
    }
    reference_upper_colour = "GREEN" if side == "LONG" else "RED"
    reference_lower_colour = "RED" if side == "LONG" else "GREEN"
    reference_upper_anchors = strongest[reference_upper_colour]
    reference_lower_anchors = strongest[reference_lower_colour]

    def straight_at(anchors, coordinate, target):
        first, last = anchors
        start, stop = first["close_ms"], last["close_ms"]
        return (first[coordinate]
                + (last[coordinate] - first[coordinate])
                * (target - start) / (stop - start))

    reference_upper = straight_at(reference_upper_anchors, "high", timestamp)
    reference_lower = straight_at(reference_lower_anchors, "low", timestamp)
    previous_upper = straight_at(reference_upper_anchors, "high", timestamp - FIVE)
    previous_lower = straight_at(reference_lower_anchors, "low", timestamp - FIVE)
    if not all(math.isfinite(x) and x > 0 for x in
               (reference_upper, reference_lower, previous_upper, previous_lower)):
        result["reason"] = "INVALID_PRICE_DATA"
        return result

    inverted = reference_upper < reference_lower
    touching = reference_upper == reference_lower
    reversed_on_signal = (previous_upper >= previous_lower and inverted)
    trend_direction = ("LONG" if side == "SHORT" else "SHORT") if inverted else side

    # Rebuild the geometry for the NEW direction after a crossover.
    # A green upper boundary always joins GREEN candle HIGHS for LONG,
    # never the green candle LOWS used by the former SHORT channel.
    upper_colour = "GREEN" if trend_direction == "LONG" else "RED"
    lower_colour = "RED" if trend_direction == "LONG" else "GREEN"
    upper_anchors = strongest[upper_colour]
    lower_anchors = strongest[lower_colour]
    upper = straight_at(upper_anchors, "high", timestamp)
    lower = straight_at(lower_anchors, "low", timestamp)
    current_previous_upper = straight_at(upper_anchors, "high", timestamp - FIVE)
    current_previous_lower = straight_at(lower_anchors, "low", timestamp - FIVE)
    ordered = upper > lower and all(
        math.isfinite(x) and x > 0
        for x in (upper, lower, current_previous_upper, current_previous_lower))
    inside = ordered and lower <= signal[3] <= upper
    # A reversal can also confirm a directional price breakout; volume flip
    # and the existing LIVE risk/entry gates still have to agree.
    confirmed_reversal_breakout = (
        ordered and reversed_on_signal and
        ((trend_direction == "LONG" and signal[3] > upper) or
         (trend_direction == "SHORT" and signal[3] < lower))
    )
    eligible_price = inside or confirmed_reversal_breakout
    if touching:
        reason = "LINES_TOUCH_TREND_UNDECIDED"
    elif not ordered:
        reason = "NEW_DIRECTION_TUNNEL_CROSSED"
    elif inverted:
        reason = "PRICE_LINES_CROSSED_TREND_REVERSED"
    elif inside:
        reason = "PRICE_INSIDE_STRAIGHT_TUNNEL"
    elif signal[3] > upper:
        reason = "PRICE_ABOVE_TUNNEL"
    else:
        reason = "PRICE_BELOW_TUNNEL"

    result.update({
        "status": "VALID",
        "reason": reason,
        "tunnel_direction": trend_direction if not touching else "NEUTRAL",
        "colour_order_changed": inverted,
        "crossing_on_signal": reversed_on_signal,
        "upper_color": upper_colour,
        "lower_color": lower_colour,
        "upper_price": upper,
        "lower_price": lower,
        "previous_upper_price": current_previous_upper,
        "previous_lower_price": current_previous_lower,
        "reference_upper_price": reference_upper,
        "reference_lower_price": reference_lower,
        "previous_close_price": previous[3],
        "signal_close_price": signal[3],
        "upper_anchors": upper_anchors,
        "lower_anchors": lower_anchors,
        "upper_slope_per_5m": upper - current_previous_upper,
        "lower_slope_per_5m": lower - current_previous_lower,
        "entry_allowed": not touching and eligible_price,
        "breakout_confirmed": confirmed_reversal_breakout,
        "anchor_selection": "TOP_2_VOLUME_BARS_PER_COLOUR_CHRONOLOGICAL",
    })
    return result


def prioritize_volume_signals(matches):
    """Strongest qualified 5m signal volume / its own previous MA20 first.

    All eligible LONGs and SHORTs compete on the same normalized scale.
    Only equal-strength signals fall back to rank 24h for deterministic ties;
    this never filters out an otherwise eligible setup.
    """
    return sorted(matches, key=lambda s: (
        -s["volume_strength_20"], s["rank"], s["inst"]
    ))


def exit_transition(pos, bars, expected_close_ms):
    """Close on first COMPLETED opposite-colour 5m candle with volume
    STRICTLY GREATER than the volume of the candle that OPENED the trade.

    New strategies: MA20 is an ENTRY filter ONLY. EXIT does not consult MA20,
    does not update reference volume while a trade is open, and does not
    depend on later same-side candles. Works for existing tracked shorts/longs
    created under the same strategy, using their saved signal snapshot.
    """
    ts = int(expected_close_ms) - FIVE
    rows = _by_ts(bars, int(expected_close_ms))
    current = rows.get(ts)
    try:
        opening_volume = float(
            pos.get("signal_volume")
            or (pos.get("signal_snapshot") or {}).get("signal_volume")
            or 0
        )
    except (ValueError, TypeError):
        opening_volume = 0
    if current is None or not math.isfinite(opening_volume) or opening_volume <= 0:
        # Fail closed on missing entry candle's volume: never invent a baseline.
        return False, opening_volume
    side = str(pos.get("side") or "")
    o, c, v = current[0], current[3], current[4]
    if side == "LONG" and c < o and v > opening_volume:
        return True, opening_volume
    if side == "SHORT" and c > o and v > opening_volume:
        return True, opening_volume
    return False, opening_volume


def legacy_exit_transition(pos, bars, expected_close_ms):
    """Preserve old, already-open V1 LONG's exact prior exit semantics."""
    ts=int(expected_close_ms)-FIVE
    rows=_by_ts(bars,int(expected_close_ms))
    current=rows.get(ts)
    mean=_previous10_mean(rows,ts)
    red=float(pos.get("last_red_volume") or pos.get("signal_red_volume") or 0)
    if current is None or mean is None or mean<=0 or current[4]>mean:
        return False,red
    if current[3]<current[0]:
        return False,current[4]
    if current[3]>current[0] and current[4]>red:
        return True,red
    return False,red



def _audit_colour(row):
    """5m candle direction by price body, NOT by the color of volume bars."""
    if row[3] > row[0]:
        return "GREEN"
    if row[3] < row[0]:
        return "RED"
    return "DOJI"


def _volume_audit(pos):
    """Durable, JSON-serializable data for research; never changes trade rules."""
    audit = pos.get("volume_candle_audit")
    if isinstance(audit, dict):
        return audit
    side = str(pos.get("side") or "").upper()
    snap = pos.get("signal_snapshot") or {}
    signal_close = int(pos.get("signal_close_ms") or snap.get("signal_close_ms") or 0)
    if not signal_close or side not in ("LONG", "SHORT"):
        return None
    audit = {
        "version": 1, "bar_minutes": 5,
        "entry_signal_candle": {
            "close_ms": signal_close,
            "color": "GREEN" if side == "LONG" else "RED",
            "volume": snap.get("signal_volume") or pos.get("signal_volume"),
        },
        # Every FULLY CLOSED bar AFTER the entry signal. The signal bar is
        # separate because it closed BEFORE the order filled.
        "candles": [], "missing_close_ms": [],
        "audited_through_close_ms": signal_close,
        "total_green_bars": 0, "total_red_bars": 0,
        "total_doji_bars": 0, "total_opposite_bars": 0,
        "total_trade_direction_bars": 0,
        "current_green_streak": 0, "current_red_streak": 0,
        "max_green_streak": 0, "max_red_streak": 0,
        "max_opposite_streak": 0,
        "exit_trigger_candle": None,
    }
    pos["volume_candle_audit"] = audit
    return audit



def _price_movement_from_candles(pos, candles):
    """Directional 5m price excursions relative to the actual broker entry.

    MFE/MAE include intra-5m high/low; the bar containing the entry fill may
    contain pre-fill extremes. Also record a stricter FULL-bars-only series.
    These are research observations, NOT simulated orders or actual TP/SL fills.
    """
    side = str(pos.get("side") or "").upper()
    try:
        entry = float(pos.get("reference_entry") or "0")
        opened = int(pos.get("opened_ms") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if side not in ("LONG", "SHORT") or not math.isfinite(entry) or entry <= 0 or opened <= 0:
        return None
    direction = 1 if side == "LONG" else -1
    approx_best = approx_worst = 0.0
    full_best = full_worst = 0.0
    max_close = min_close = 0.0
    approximate_best_price = approximate_worst_price = entry
    first_partial = False
    counted_approx = counted_full = missing = 0
    for row in candles:
        bar_end = int(row["close_ms"])
        bar_start = bar_end - FIVE
        if bar_end <= opened:
            row["price_exposure"] = "BEFORE_ENTRY"
            continue
        partial = bar_start < opened
        row["price_exposure"] = "ENTRY_PARTIAL" if partial else "FULL"
        if partial:
            first_partial = True
        if row.get("high") is None or row.get("low") is None or row.get("close") is None:
            missing += 1
            continue
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        if not all(math.isfinite(x) and x > 0 for x in (high,low,close)):
            missing += 1
            continue
        # Profit-direction gains and adverse reversal, as POSITIVE percent
        # for LONG and SHORT, with zeros when no excursion occurred.
        f_price = high if direction == 1 else low
        a_price = low if direction == 1 else high
        favourable = max(0.0, direction * (f_price / entry - 1.0) * 100.0)
        adverse = max(0.0, -direction * (a_price / entry - 1.0) * 100.0)
        close_return = direction * (close / entry - 1.0) * 100.0
        row["position_return_close_pct"] = round(close_return, 6)
        row["favorable_in_bar_pct_approx"] = round(favourable, 6)
        row["adverse_in_bar_pct_approx"] = round(adverse, 6)
        counted_approx += 1
        max_close = max(max_close, close_return)
        min_close = min(min_close, close_return)
        if favourable > approx_best:
            approx_best = favourable
            approximate_best_price = f_price
        if adverse > approx_worst:
            approx_worst = adverse
            approximate_worst_price = a_price
        if not partial:
            counted_full += 1
            full_best = max(full_best, favourable)
            full_worst = max(full_worst, adverse)
    # Recomputed on every confirmed scan, safe against a delayed candle
    # and safe after restarting GitHub Actions.
    return {
        "version":1, "side":side, "entry_price":entry,
        "opened_ms":opened, "bar_minutes":5,
        "fully_observed_candles":counted_full,
        "observed_candles_including_entry_partial":counted_approx,
        "candles_missing_ohlc":missing,
        "contains_partial_entry_candle":first_partial,
        "max_favorable_move_pct_approx":round(approx_best,6),
        "max_adverse_move_pct_approx":round(approx_worst,6),
        "max_favorable_move_pct_full_bars_only":round(full_best,6),
        "max_adverse_move_pct_full_bars_only":round(full_worst,6),
        "best_price_5m_approx":approximate_best_price,
        "worst_price_5m_approx":approximate_worst_price,
        "best_close_return_pct":round(max_close,6),
        "worst_close_return_pct":round(min_close,6),
        "limitations":"5m OHLC extremes within entry candle may have occurred before fill; 5m bars cannot determine high/low order or true TP-vs-SL first touch. Missing bars are not estimated.",
    }


def _audit_confirmed_volume_candles(pos, bars, expected_close_ms):
    """Capture every available confirmed 5m candle; dedupe and identify gaps.

    Recalculating counters from recorded candles allows out-of-order gap
    recovery after a delayed API response. A gap breaks a consecutive streak;
    a missing bar is NEVER counted as a green or red bar.
    No additional market or account requests are made.
    """
    if pos.get("strategy") != STRATEGY:
        return None
    audit = _volume_audit(pos)
    if audit is None:
        return None
    start = int(audit["entry_signal_candle"]["close_ms"])
    close = int(expected_close_ms)
    if close <= start:
        return audit
    confirmed = _by_ts(bars, close)
    previous = {
        int(row["close_ms"]): row for row in audit["candles"]
        if isinstance(row, dict) and row.get("close_ms")
    }
    for ts in range(start + FIVE, close + 1, FIVE):
        candle = confirmed.get(ts - FIVE)
        if candle is None:
            continue
        if ts not in previous:
            previous[ts] = {
                "close_ms": ts, "color": _audit_colour(candle),
                "volume": candle[4],
                "open": candle[0], "high": candle[1],
                "low": candle[2], "close": candle[3],
            }
        else:
            # Earlier saved audits may contain volume, open and close but
            # no OHLC highs/lows: fill only missing price fields when available.
            previous[ts].setdefault("high", candle[1])
            previous[ts].setdefault("low", candle[2])
    audit["candles"] = [previous[ts] for ts in sorted(previous) if start < ts <= close]
    # A skipped/missing 5m candle is visible as a gap in the JSON, not silently
    # treated as a candle of the preferred direction.
    audit["missing_close_ms"] = [
        ts for ts in range(start + FIVE, close + 1, FIVE) if ts not in previous
    ]
    audit["audited_through_close_ms"] = close
    green = red = doji = 0
    green_streak = red_streak = 0
    max_green = max_red = max_opposite = 0
    last_close = start
    side = str(pos.get("side") or "").upper()
    for item in audit["candles"]:
        if int(item["close_ms"]) - last_close != FIVE:
            green_streak = red_streak = 0
        last_close = int(item["close_ms"])
        col = item["color"]
        if col == "GREEN":
            green += 1
            green_streak += 1
            red_streak = 0
        elif col == "RED":
            red += 1
            red_streak += 1
            green_streak = 0
        else:
            doji += 1
            green_streak = red_streak = 0
        max_green = max(max_green, green_streak)
        max_red = max(max_red, red_streak)
        max_opposite = max(max_opposite, red_streak if side == "LONG" else green_streak)
    audit.update({
        "total_green_bars": green, "total_red_bars": red,
        "total_doji_bars": doji,
        "total_opposite_bars": red if side == "LONG" else green,
        "current_green_streak": green_streak, "current_red_streak": red_streak,
        "total_trade_direction_bars": green if side == "LONG" else red,
        "max_green_streak": max_green, "max_red_streak": max_red,
        "max_opposite_streak": max_opposite,
    })
    movement = _price_movement_from_candles(pos, audit["candles"])
    if movement is not None:
        pos["price_movement"] = movement
    return audit


def pivot_long_entry_filter(sig, quote):
    """Check frozen LONG TP/SL line distances; MA10 does not gate entry."""
    audit = {"rule": "LONG_TP060_SL010_NO_MA10_V2", "allowed": False,
             "max_tp_gap_pct": 0.60, "min_sl_gap_pct": 0.10}
    if sig.get("side") != "LONG":
        return dict(audit, reason="PIVOT_LONG_ONLY")
    try:
        # Preserve the existing confirmed-indicator safety check. Only the
        # MA10 price-distance entry threshold has been removed.
        if (sig.get("indicator_status") != "OK"
                or sig.get("indicator_timeframe") != "5m"
                or int(sig["indicator_signal_close_ms"]) != int(sig["signal_close_ms"])):
            return dict(audit, reason="LONG_FILTER_INDICATORS_UNAVAILABLE")
        tunnel = sig["volume_price_tunnel"]
        price = Decimal(str(quote["expected_fill"]))
        tp = Decimal(str(tunnel["upper_price"]))
        sl = Decimal(str(tunnel["lower_price"]))
        if not all(x.is_finite() and x > 0 for x in (price, tp, sl)):
            return dict(audit, reason="LONG_FILTER_INVALID_PRICES")
        if not sl < price < tp:
            return dict(audit, reason="LONG_FILTER_PRICE_OUTSIDE_TUNNEL")
        audit.update(expected_fill=str(price),
                     tp_line=str(tp), sl_line=str(sl),
                     tp_gap_pct=float((tp-price)/price*100),
                     sl_gap_pct=float((price-sl)/price*100))
        # Keep both TP/SL limits inclusive; MA10 is not an entry constraint.
        if tp > price * Decimal("1.006"):
            return dict(audit, reason="LONG_TP_GAP_ABOVE_060")
        if sl > price * Decimal("0.999"):
            return dict(audit, reason="LONG_SL_GAP_BELOW_010")
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return dict(audit, reason="LONG_FILTER_DATA_UNAVAILABLE")
    return dict(audit, allowed=True, reason="PASS")


def _open_one(bot, state, sig, instruments):
    inst = sig["inst"]
    side = sig["side"]
    if side not in ("LONG","SHORT"):
        return False
    pivot_mode = PIVOT_ENABLED or bool(sig.get("pivot_tunnel_entry"))
    if bot.risk_stop_active(state):
        return False
    if bot.get_tracked_positions(state).get(inst):
        return False
    if not (0 <= bot.now_ms()-int(sig["signal_close_ms"]) <= min(bot.SIGNAL_MAX_AGE_MS, AGE_MAX_MS)):
        return False
    meta = instruments.get(inst)
    if not meta:
        return False
    broker = bot.get_open_positions()
    open_insts = {str(x.get("instId")) for x in broker}
    tracked = bot.get_tracked_positions(state)
    if open_insts - set(tracked):
        # Never submit an order if there are positions not tracked by this bot.
        return False
    if len(open_insts) >= min(MAX_POSITIONS, bot.MAX_OPEN_POSITIONS):
        return False
    if inst in open_insts:
        return False

    # Reserve capital across all tracked/account positions. Do NOT count the
    # full shared bankroll anew for every independent symbol.
    bankroll = bot.current_live_bankroll(state)
    committed = sum(
        (bot.d(p.get("notional_usdt") or "0") for p in tracked.values()),
        Decimal("0"),
    )
    available = bot.get_available_usdt()
    remaining = min(bot.MAX_NOTIONAL_USDT, bankroll) - committed
    spendable = min(remaining, available-CASH_BUFFER_USDT)
    if spendable < MIN_PER_COIN_USDT:
        print(f"{STRATEGY}: insufficient free bankroll for {inst}: {spendable}")
        return False

    # Shared test capital: up to 20 USDT total across four positions.
    # Each new order uses at most 5 USDT while respecting exchange funds,
    # the shared cap, and the cash buffer.
    target = min(MAX_PER_COIN_USDT, spendable)
    quote = bot.checked_preorder_quote(inst, side, sig["close"],
                                      enforce_adverse_gap=False)
    quote_rejection = short_entry_extra_rejection(side, quote)
    if quote_rejection:
        # Reject before market order POST; broker protection stays untouched.
        sig["preorder_rejection"] = quote_rejection
        sig["pre_order_quote"] = quote
        print(f"{STRATEGY}: SHORT {inst} blocked: {quote_rejection} "
              f"(adverse_gap={quote.get('adverse_gap_pct')}%, "
              f"spread={quote.get('spread_pct')}%)")
        return False
    # Reject invalid/non-tradable tunnel targets BEFORE any market order.
    # If the fill unexpectedly crosses a line, protect that filled position
    # with the existing fixed emergency levels rather than leaving it naked.
    tunnel = sig.get("volume_price_tunnel") or {}
    if (tunnel.get("status") != "VALID"
            or tunnel.get("tunnel_direction") != side
            or not tunnel.get("entry_allowed")):
        sig["preorder_rejection"] = "TUNNEL_TPSL_NOT_VALID"
        return False
    try:
        frozen_tp = tunnel["upper_price"] if side == "LONG" else tunnel["lower_price"]
        frozen_sl = tunnel["lower_price"] if side == "LONG" else tunnel["upper_price"]
        tunnel_pos = {
            "side":side, "reference_entry":str(quote["expected_fill"]),
            "tp_sl_mode":(SECOND_BOUNCE_SL_MODE if sig.get("second_bounce_entry") else TUNNEL_TPSL_MODE),
            "tunnel_tp_line":str(frozen_tp),
            "tunnel_sl_line":str(frozen_sl),
        }
        _protection_prices(bot,tunnel_pos,meta)
    except (KeyError, ValueError, TypeError, ArithmeticError, RuntimeError) as exc:
        sig["preorder_rejection"] = "TUNNEL_TPSL_UNTRADEABLE"
        sig["preorder_tpsl_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return False
    if pivot_mode and side == "LONG":
        sig["pre_order_quote"] = quote
        sig["pivot_long_entry_filter"] = pivot_long_entry_filter(sig, quote)
        if not sig["pivot_long_entry_filter"]["allowed"]:
            sig["preorder_rejection"] = sig["pivot_long_entry_filter"]["reason"]
            print(f"{STRATEGY}: LONG {inst} blocked: {sig['preorder_rejection']}")
            return False
    sized = bot.size_for_notional(bot.d(quote["expected_fill"]), meta, target)
    if not sized:
        print(f"{STRATEGY}: no valid size for {inst} with cap {target}")
        return False
    size, expected_notional = sized

    bot.set_one_x(inst)
    order_id = ("volreverse" + uuid.uuid4().hex)[:32]
    # Durable per-coin order latch must be saved BEFORE broker POST.
    attempt = {
        "inst":inst, "signal_close_ms":int(sig["signal_close_ms"]),
        "status":"POST_STARTED", "client_order_id":order_id,
        "started_ms":bot.now_ms(),
    }
    state.setdefault("volume_long_order_attempts", {})[inst] = attempt
    bot.save_state(state)
    state["_volume_long_post_started"] = True
    try:
        result = bot.private_request("POST", "/api/v1/trade/order", body={
            "instId":inst,"marginMode":bot.MARGIN_MODE,
            "positionSide":"net","side":"buy" if side=="LONG" else "sell","orderType":"market",
            "size":bot.clean_decimal(size),"reduceOnly":"false",
            "clientOrderId":order_id,
        })
    except Exception:
        attempt["status"] = "UNCERTAIN"
        bot.save_state(state)
        raise
    row = result[0] if isinstance(result,list) and result else (result or {})
    if str(row.get("code","0")) != "0":
        attempt["status"] = "UNCERTAIN"
        bot.save_state(state)
        raise RuntimeError("Broker did not affirm market order: "+str(row)[:300])
    attempt["status"]="BROKER_ACCEPTED"
    now = bot.now_ms()
    pos = {
        "inst":inst,"side":side,"opened_ms":now,
        "signal_close_ms":int(sig["signal_close_ms"]),
        "signal_age_ms":now-int(sig["signal_close_ms"]),
        "signal_rank":int(sig["rank"]),
        "signal_snapshot":dict(sig,pre_order_quote=quote),
        "strategy":STRATEGY,
        "hold_policy":("PIVOT_5M_CLOSE_BREAK" if sig.get("pivot_tunnel_entry") else HOLD_POLICY),
        "pivot_tunnel":sig.get("pivot_tunnel_info"),
        "last_same_volume":sig["signal_volume"],
        "last_checked_signal_close_ms":int(sig["signal_close_ms"]),
        "order_id":str(row.get("orderId") or ""),
        "client_order_id":order_id,
        "size":bot.clean_decimal(size),
        "reference_entry":"",
        "filled_size":"",
        "notional_usdt":bot.clean_decimal(expected_notional),
        "tp":"","sl":"","tp_policy":"TUNNEL_LINE","sl_policy":"TUNNEL_LINE",
        "tp_sl_mode":(SECOND_BOUNCE_SL_MODE if sig.get("second_bounce_entry") else TUNNEL_TPSL_MODE),
        "second_bounce_exit": bool(sig.get("second_bounce_entry")),
        "second_bounce_upper_count": 0,
        "second_bounce_last_upper_pivot_ms": 0,
        "second_bounce_lower_count": 0,
        "second_bounce_last_lower_pivot_ms": 0,
        "tunnel_tp_line":str(frozen_tp),
        "tunnel_sl_line":str(frozen_sl),
        "tunnel_reference_close_ms":int(sig["signal_close_ms"]),
        "risk_profile":("1X_PIVOT_TUNNEL_LINES" if sig.get("pivot_tunnel_entry")
                        else "1X_VOLUME_FLIP_TUNNEL_LINES"),
        "protection_status":"AWAITING_VERIFIED_FILL",
        "code_commit":bot.CODE_COMMIT,
    }
    _volume_audit(pos)  # Persist signal-volume baseline from the first LIVE fill.
    bot.get_tracked_positions(state)[inst]=pos
    bot.save_state(state)
    fill = bot.wait_for_order_fill(inst,pos["order_id"],order_id)
    if not fill or bot.d(fill.get("averagePrice") or "0")<=0 or bot.d(fill.get("filledSize") or "0")<=0:
        # Do not send new unrelated orders until the fill can be reconciled.
        attempt["status"]="UNCERTAIN_FILL"
        bot.notify(f"CRITICAL: {inst} LONG accepted but fill not verified; reconcile broker.",
                   "BloFin LIVE volume LONG safety")
        bot.save_state(state)
        return True
    actual_price=bot.d(fill["averagePrice"])
    actual_size=bot.d(fill["filledSize"])
    contract_value=bot.d(meta.get("contractValue") or "0")
    pos.update({"reference_entry":bot.clean_decimal(actual_price),
        "filled_size":bot.clean_decimal(actual_size),
        "notional_usdt":bot.clean_decimal(actual_price*actual_size*contract_value),
        "entry_fee":str(fill.get("fee") or "")})
    # Set the broker-confirmed price baseline before the first subsequent bar.
    pos["price_movement"] = _price_movement_from_candles(
        pos, pos["volume_candle_audit"]["candles"]
    )
    attempt["status"]="PROTECTING"
    bot.save_state(state)
    # Fill may differ from the checked quote. Do not place a broker TP/SL
    # on the wrong side of the actual fill. Existing 0.55/0.50 levels
    # remain the emergency protection in this exceptional case only.
    try:
        _protection_prices(bot,pos,meta)
    except (ValueError, TypeError, ArithmeticError, RuntimeError) as exc:
        pos["tp_sl_mode"] = EMERGENCY_TPSL_MODE
        pos["risk_profile"] = "1X_EMERGENCY_FIXED_AFTER_TUNNEL_FILL"
        pos["tunnel_protection_fallback"] = f"{type(exc).__name__}: {exc}"[:200]
        bot.save_state(state)
        bot.notify(f"WARNING {side} {inst}: actual fill crossed tunnel TP/SL; "
                   "using emergency fixed broker TP +0.55% / SL -0.50%.",
                   "BloFin LIVE tunnel fill protection")
    if not _ensure_protection(bot,state,pos,meta):
        attempt["status"]="PROTECTION_UNCERTAIN"
        bot.save_state(state)
        _emergency_close_unprotected(bot, state, inst)
        bot.notify(f"CRITICAL {side} {inst}: TP/SL unverified; "
                   "emergency broker-verified close attempted.",
                   "BloFin LIVE TPSL SAFETY")
        return True
    attempt["status"]="TRACKED"
    bot.save_state(state)
    try:
        from blofin_trade_charts import capture_chart
        pos["entry_chart"]=capture_chart(bot,pos,"ENTRY",now,actual_price)
    except Exception as exc:
        pos["entry_chart_error"]=f"{type(exc).__name__}: {exc}"
    bot.notify(
        f"OPEN {side} {inst} | rank {sig['rank']}/200 | vol ratio {sig['volume_ratio'] or 0:.2f}x "
        f"| 1x isolated, notional {pos['notional_usdt']} USDT | "
        f"TP {pos['tp']} / SL {pos['sl']} (Last Price, {pos.get('tp_sl_mode')}) | " +
        ("EXIT: broker TP/SL or CLOSED 5m breaks last confirmed swing."
         if sig.get("pivot_tunnel_entry") else
         "EXIT: broker TP/SL or opposite CLOSED 5m volume > ENTRY candle volume."),
        "BloFin LIVE both sides volume flip",
        attachment_path=pos.get("entry_chart"),
    )
    return True



def _emergency_close_unprotected(bot, state, inst, broker_rows=None):
    """Close a verified bot-owned unprotected fill; never an unknown position."""
    pos = bot.get_tracked_positions(state).get(inst)
    if not pos or pos.get("exit_pending"):
        return False
    try:
        rows = broker_rows if broker_rows is not None else bot.get_open_positions()
        matches = [r for r in rows if str(r.get("instId") or "") == inst]
        if len(matches) != 1:
            raise RuntimeError("Broker position absent or ambiguous")
        row = matches[0]
        signed = bot.d(row.get("positions") or "0")
        original = bot.d(pos.get("filled_size") or "0")
        if not (signed.is_finite() and original.is_finite() and original > 0
                and signed != 0 and abs(signed) <= original):
            raise RuntimeError("Position size not verified")
        if ((signed > 0) != (pos.get("side") == "LONG")
                or str(row.get("marginMode") or "").lower() != "isolated"
                or str(row.get("positionSide") or "").lower() != "net"):
            raise RuntimeError("Broker position identity mismatch")
        created = int(row.get("createTime") or 0)
        if created and created > int(pos.get("opened_ms") or 0) + 120000:
            raise RuntimeError("Broker position was reopened")
    except Exception as exc:
        pos["emergency_close_error"]=str(exc)[:240]
        bot.save_state(state)
        bot.notify(f"CRITICAL {inst}: TP/SL unverified; safe close requires "
                   f"reconciliation: {exc}", "BloFin LIVE TPSL SAFETY")
        return False
    pos["exit_pending"]="POST_STARTED"
    pos["exit_retry_count"]=0
    pos["exit_retry_next_ms"]=int(bot.now_ms())+60000
    bot.save_state(state)
    try:
        bot._run_for_tracked_position(
            state, inst, bot.close_tracked_position,
            "EMERGENCY CLOSE: exchange TP/SL rejected or unverified")
        remaining=bot.get_tracked_positions(state).get(inst)
        if remaining:
            remaining["exit_pending"]="AWAITING_BROKER_FLAT"
        bot.save_state(state)
        return True
    except Exception as exc:
        remaining=bot.get_tracked_positions(state).get(inst)
        if remaining:
            remaining["exit_pending"]="UNCERTAIN_CLOSE"
            remaining["emergency_close_error"]=str(exc)[:240]
        bot.save_state(state)
        bot.notify(f"CRITICAL {inst}: emergency close uncertain; broker "
                   f"reconciliation required: {exc}", "BloFin LIVE TPSL SAFETY")
        return False


def _reconcile_pending_volume_closes(bot, state, broker_rows, scan):
    """Retry a pending *close only* after a fresh account-side broker snapshot.

    A failed/uncertain POST is never repeated solely because a timer elapsed.
    The snapshot must show the original same-direction net position, isolated
    margin, and a size no greater than the bot's verified original fill.
    This also makes previously stuck 429 exits from older bot runs recoverable.
    """
    active = bot.get_tracked_positions(state)
    now = int(bot.now_ms())
    by_inst = {}
    for row in broker_rows:
        by_inst.setdefault(str(row.get("instId") or ""), []).append(row)
    for inst, pos in list(active.items()):
        if not pos.get("exit_pending") or pos.get("strategy") != STRATEGY:
            continue
        rows = by_inst.get(inst, [])
        if len(rows) != 1:
            # No position: reconciliation in sync_all_tracked_positions must
            # finish via verified broker history, not a duplicate close.
            # Multiple positions: unsafe to issue "close entire net position".
            scan["rejections"].append({"inst":inst,
                "reason":"PENDING_EXIT_BROKER_ABSENT_OR_AMBIGUOUS"})
            continue
        row = rows[0]
        try:
            from decimal import Decimal
            signed = Decimal(str(row["positions"]))
            original = Decimal(str(pos.get("filled_size") or pos.get("size") or "0"))
            if not signed.is_finite() or not original.is_finite() or original <= 0:
                raise ValueError("missing verified original size or broker quantity")
            if signed == 0:
                raise ValueError("broker quantity is zero")
            broker_side = "LONG" if signed > 0 else "SHORT"
            if str(row.get("positionSide") or "").lower() != "net":
                raise ValueError("position mode is not net")
            if str(row.get("marginMode") or "").lower() != "isolated":
                raise ValueError("margin mode is not isolated")
            if broker_side != str(pos.get("side") or "").upper():
                raise ValueError("broker side differs from tracked position")
            if abs(signed) > original:
                raise ValueError("broker size exceeds originally verified fill")
            broker_created = int(row.get("createTime") or 0)
            opened_ms = int(pos.get("opened_ms") or 0)
            if broker_created and opened_ms and broker_created > opened_ms + 120000:
                raise ValueError("broker position was reopened since bot entry")
        except (TypeError, ValueError, ArithmeticError) as exc:
            scan["rejections"].append({"inst":inst,
                "reason":"PENDING_EXIT_BROKER_IDENTITY_MISMATCH",
                "error":str(exc)[:200]})
            print(f"{STRATEGY}: BLOCK CLOSE RETRY {inst}: {exc}")
            continue
        if now < int(pos.get("exit_retry_next_ms") or 0):
            scan["rejections"].append({"inst":inst,
                "reason":"PENDING_EXIT_RETRY_BACKOFF"})
            continue
        # Persist BEFORE sending, to prevent a restart/re-run from rapidly
        # repeating an HTTP request whose result may not yet be visible.
        attempt = int(pos.get("exit_retry_count") or 0) + 1
        pos["exit_retry_count"] = attempt
        pos["exit_retry_last_ms"] = now
        pos["exit_retry_next_ms"] = now + min(600000, 60000 * (2 ** min(attempt - 1, 4)))
        pos["exit_pending"] = "RETRY_POST_STARTED"
        bot.save_state(state)
        try:
            bot._run_for_tracked_position(
                state, inst, bot.close_tracked_position,
                "RECOVERED VOLUME EXIT: broker-verified pending close")
            # record_closed may leave position tracked while exchange
            # history is pending. Do not permit new entries until flat.
            if inst in bot.get_tracked_positions(state):
                bot.get_tracked_positions(state)[inst]["exit_pending"] = "AWAITING_BROKER_FLAT"
            scan["exits"].append(inst)
            print(f"{STRATEGY}: CLOSE RETRY ACCEPTED {inst}, attempt {attempt}")
        except Exception as exc:
            still = bot.get_tracked_positions(state).get(inst)
            if still:
                still["exit_pending"] = "UNCERTAIN_CLOSE"
                still["exit_retry_error"] = f"{type(exc).__name__}: {exc}"[:240]
            scan["rejections"].append({"inst":inst,
                "reason":"PENDING_EXIT_RETRY_FAILED",
                "error":f"{type(exc).__name__}: {exc}"[:240]})
            bot.notify(f"CRITICAL {inst}: broker-verified close retry attempt "
                       f"{attempt} failed: {exc}; next attempt after cooldown. "
                       "Trading remains blocked until reconciliation.",
                       "BloFin LIVE volume exit safety")
        bot.save_state(state)


def run(bot, state, top200, tickers, instruments, expected_close_ms, wait_for_confirmed_close):
    if bot.SIGNAL_MINUTES!=5 or bot.TOP_N!=200 or len(top200)!=200:
        raise RuntimeError("Volume flip LONG/SHORT requires confirmed 5m and TOP200")
    if int(state.get("last_scan_close_ms") or 0)==expected_close_ms:
        print(f"{STRATEGY}: duplicate scan skipped")
        return
    scan={"strategy":STRATEGY,"selection_priority":"SIGNAL_VOLUME_DIV_PREVIOUS_20_BAR_MEAN_DESC",
          "entry_volume_trend_filter":"DAY_UK_DOMINANT_COLOUR_VOLUME",
          "entry_price_tunnel_filter":("FOUR_CONFIRMED_PRICE_PIVOTS"
                                      if PIVOT_ENABLED else
                                      "STRAIGHT_COLOUR_VOLUME_PRICE_TUNNEL_20BAR_INSIDE"),
          "volume_entry_strength_range_filter":"DISABLED",
          "long_entry_rank_range_inclusive":[1,LONG_ENTRY_MAX_RANK],
          "long_requires_falling_prior_20bar_mean":True,
          "short_entry_min_adverse_gap_pct_inclusive":float(SHORT_ENTRY_MIN_ADVERSE_GAP_PCT),
          "short_entry_max_spread_pct_inclusive":float(SHORT_ENTRY_MAX_SPREAD_PCT),
          "signal_close_ms":int(expected_close_ms),
          "top200_count":len(top200),"status":"PENDING",
          "scanned_confirmed":0,"matched":0,"candidates":[],
          "entries":[],"exits":[],"sl_updates":[],"rejections":[]}

    bot.sync_all_tracked_positions(state)
    bot.evaluate_all_tracked_exit_signals(state, expected_close_ms)

    active = bot.get_tracked_positions(state)
    broker = bot.get_open_positions()
    broker_set={str(p.get("instId")) for p in broker}
    # An exchange-side TP/SL may have already flattened a pivot-tunnel trade.
    # Re-arm only after a fresh four-point configuration; never reuse old pivots.
    pivot_open = state.setdefault("pivot_tunnel_live_positions", {})
    pivot_restart = state.setdefault("pivot_tunnel_restart_after", {})
    for old_inst in list(pivot_open):
        if old_inst not in active and old_inst not in broker_set:
            pivot_restart[old_inst] = int(expected_close_ms)
            del pivot_open[old_inst]
    unprotected=[]
    # Upgrade already-open V1 positions using their actual filled entry price.
    for inst,pos in list(active.items()):
        if (inst in broker_set and pos.get("strategy")==STRATEGY
                and not pos.get("exit_pending")):
            try:
                if pos.get("tunnel_sl_pending"):
                    _reconcile_pending_tunnel_sl(bot,state,pos)
                if pos.get("tunnel_tp_pending"):
                    _reconcile_pending_tunnel_tp(bot,state,pos)
                # An in-flight amend can already be live at the broker. Do
                # not mistake its OLD local price for missing protection and
                # emergency-close a still protected trade on a stale read.
                if pos.get("tunnel_sl_pending") or pos.get("tunnel_tp_pending"):
                    unprotected.append(inst)
                    scan["rejections"].append({
                        "inst":inst,"reason":"TPSL_AMEND_AWAITING_BROKER_VERIFICATION"})
                    continue
                protected=_ensure_protection(bot,state,pos,instruments.get(inst))
            except Exception as exc:
                pos["protection_error"]=f"{type(exc).__name__}: {exc}"[:250]
                bot.save_state(state)
                if pos.get("tunnel_sl_pending") or pos.get("tunnel_tp_pending"):
                    unprotected.append(inst)
                    scan["rejections"].append({
                        "inst":inst,"reason":"TPSL_AMEND_BROKER_READ_UNCERTAIN",
                        "error":pos["protection_error"]})
                    continue
                protected=False
            if protected:
                if pos.get("trigger_migration_deferred"):
                    # Old MARK OCO is still active; block NEW trading until
                    # the live exchange confirms conversion to LAST.
                    unprotected.append(inst)
                    scan["rejections"].append({
                        "inst":inst,"reason":"TRIGGER_LAST_AMEND_UNVERIFIED"})
                attempt=(state.get("volume_long_order_attempts") or {}).get(inst)
                if attempt and attempt.get("status") in ("PROTECTING","PROTECTION_UNCERTAIN"):
                    attempt["status"]="TRACKED"
                    bot.save_state(state)
            else:
                unprotected.append(inst)
                _emergency_close_unprotected(bot, state, inst, broker)
                bot.notify(f"CRITICAL {inst}: broker TP/SL not verified; "
                           "no additional entries until resolved.",
                           "BloFin LIVE TPSL SAFETY")
    # Clear protection uncertainty only after the broker reports a flat position.
    for inst,attempt in (state.get("volume_long_order_attempts") or {}).items():
        if (attempt.get("status") in ("PROTECTING","PROTECTION_UNCERTAIN")
                and inst not in broker_set and inst not in active):
            attempt["status"]="SAFE_CLOSED"
    # Preserve old positions' TP/SL. Existing old positions stay managed by
    # their prior policy; do not change, cancel or reprice their orders.
    untracked=broker_set-set(active)
    if untracked:
        scan["status"]="BLOCK_UNTRACKED_ACCOUNT_POSITIONS"
        scan["untracked"]=sorted(untracked)
    pending=state.get("volume_long_order_attempts") or {}
    uncertain=[i for i,a in pending.items()
               if a.get("status") in ("POST_STARTED","UNCERTAIN","BROKER_ACCEPTED","UNCERTAIN_FILL","PROTECTING","PROTECTION_UNCERTAIN")]
    old_attempt=state.get("red_sma_order_attempt") or {}
    if old_attempt.get("status") in ("POST_STARTED","UNCERTAIN","ACCEPTED_BROKER"):
        uncertain.append("OLD_RED_ORDER")
    if uncertain or unprotected:
        scan["status"]="BLOCK_UNCERTAIN_ORDERS"
        scan["unprotected"]=unprotected
        scan["uncertain_instruments"]=uncertain

    # Prioritise CLOSE reconciliation over hundreds of public candle calls:
    # for already pending positions, a fresh broker snapshot was obtained above.
    _reconcile_pending_volume_closes(bot, state, broker, scan)
    active = bot.get_tracked_positions(state)
    unresolved = any(p.get("exit_pending") for p in active.values())
    # While any close remains uncertain, suspend public TOP200 candle traffic.
    # This preserves API quota for the urgent broker close and forbids new trades.
    requested=(list(active) if unresolved else
               list(dict.fromkeys(list(top200)+list(active))))
    confirmed = wait_for_confirmed_close(requested, expected_close_ms) or {}
    scan["scanned_confirmed"]=sum(1 for inst in top200 if inst in confirmed)
    # Maintain every observable coin's UK-day volume regime even while entries
    # are paused or risk-blocked. This is observational, never changes exits.
    volume_trends = state.setdefault("volume_trends_uk_daily", {})
    day_start = _volume_trend_midnight_ms(expected_close_ms)
    for inst in list(volume_trends):
        if (not isinstance(volume_trends[inst], dict)
                or volume_trends[inst].get("day_start_ms") != day_start):
            del volume_trends[inst]
    confirmed_trends = {}
    for inst in top200:
        bars = confirmed.get(inst)
        if not bars or int(bot.bar_close_ms(bars[-1])) != expected_close_ms:
            continue
        trend = volume_trend_from_bars(
            bars, expected_close_ms, volume_trends.get(inst))
        if trend is not None:
            volume_trends[inst] = trend
            confirmed_trends[inst] = trend
    scan["volume_trends_confirmed"] = len(confirmed_trends)
    scan["volume_trend_backfill_attempts"] = 0
    scan["volume_trend_backfill_successes"] = 0
    scan["volume_trends_long"] = sum(x["trend"] == "LONG" for x in confirmed_trends.values())
    scan["volume_trends_short"] = sum(x["trend"] == "SHORT" for x in confirmed_trends.values())
    # Archive all confirmed 5m candles for tracked trades, including trades
    # waiting for broker close reconciliation. No new BloFin API calls.
    for inst, pos in list(active.items()):
        bars = confirmed.get(inst)
        if (pos.get("strategy")==STRATEGY and bars and
                int(bot.bar_close_ms(bars[-1]))==expected_close_ms):
            _audit_confirmed_volume_candles(pos, bars, expected_close_ms)
    for inst,pos in list(active.items()):
        legacy = pos.get("strategy")==LEGACY_STRATEGY and pos.get("hold_policy")==LEGACY_HOLD_POLICY
        if not legacy and not (pos.get("strategy")==STRATEGY
                               and (pos.get("hold_policy")==HOLD_POLICY
                                    or pos.get("pivot_tunnel"))):
            continue
        if pos.get("exit_pending"):
            continue
        if int(pos.get("signal_close_ms") or 0)>=expected_close_ms:
            continue
        if int(pos.get("last_checked_signal_close_ms") or 0)>=expected_close_ms:
            continue
        bars=confirmed.get(inst)
        if not bars or int(bot.bar_close_ms(bars[-1])) != expected_close_ms:
            scan["rejections"].append({"inst":inst,"reason":"NO_CONFIRMED_EXIT_CANDLE"})
            continue
        if pos.get("second_bounce_exit"):
            should_close = second_boundary_bounce(pos, _by_ts(bars,expected_close_ms),expected_close_ms)
            new_volume = pos.get("last_same_volume")
        elif pos.get("pivot_tunnel"):
            should_close = pivot_exit_transition(
                pos["pivot_tunnel"], _by_ts(bars, expected_close_ms),
                expected_close_ms, pos["side"])
            new_volume = pos.get("last_same_volume")
        else:
            should_close,new_volume=(legacy_exit_transition(pos,bars,expected_close_ms)
                                     if legacy else exit_transition(pos,bars,expected_close_ms))
        pos["last_red_volume" if legacy else "last_same_volume"]=new_volume
        pos["last_checked_signal_close_ms"]=int(expected_close_ms)
        if not should_close and pos.get("pivot_tunnel"):
            try:
                trail = _trail_confirmed_pivot_tpsl(
                    bot, state, pos, instruments.get(inst),
                    _by_ts(bars, expected_close_ms), expected_close_ms, tickers.get(inst))
                if trail:
                    scan["sl_updates"].append({"inst":inst, **trail})
            except Exception as exc:
                pos["sl_trail_error"]=f"{type(exc).__name__}: {exc}"[:220]
                scan["rejections"].append({"inst":inst,"reason":"TRAIL_SL_ERROR",
                                           "error":pos["sl_trail_error"]})
                bot.save_state(state)
        if should_close:
            if pos.get("pivot_tunnel"):
                pivot_restart[inst] = int(expected_close_ms)
            # Archive the EXACT confirmed trigger bar BEFORE submitting close.
            # In 429 recovery, this remains the original trigger candle;
            # a later broker fill must not overwrite the historic trigger.
            if not legacy:
                audit = _volume_audit(pos)
                signal_bar = _by_ts(bars, expected_close_ms).get(expected_close_ms-FIVE)
                if audit is not None and signal_bar is not None and audit.get("exit_trigger_candle") is None:
                    audit["exit_trigger_candle"] = {
                        "close_ms": int(expected_close_ms),
                        "color": _audit_colour(signal_bar),
                        "volume": signal_bar[4],
                        "open": signal_bar[0],
                        "close": signal_bar[3],
                        "entry_signal_volume": (pos.get("signal_snapshot") or {}).get("signal_volume"),
                    }
            # Persist a close intent BEFORE the POST; after any ambiguous
            # response, retry only after a fresh, same-side broker verification.
            if not legacy:
                pos["exit_pending"] = "POST_STARTED"
                pos["exit_retry_count"] = 0
                pos["exit_retry_next_ms"] = bot.now_ms() + 60000
                bot.save_state(state)
            try:
                bot._run_for_tracked_position(
                    state,inst,bot.close_tracked_position,
                    ("PIVOT TUNNEL EXIT: 5m close beyond confirmed swing"
                     if pos.get("pivot_tunnel") else
                     "LEGACY VOLUME: GREEN vol > last RED" if legacy else
                     "VOLUME EXIT: opposite candle vol > opening signal candle vol"))
                if inst in bot.get_tracked_positions(state):
                    bot.get_tracked_positions(state)[inst]["exit_pending"]="AWAITING_BROKER_FLAT"
                scan["exits"].append(inst)
            except Exception as exc:
                tracked = bot.get_tracked_positions(state).get(inst)
                if tracked is not None:
                    tracked["exit_pending"]="UNCERTAIN_CLOSE"
                    tracked["exit_retry_error"]=f"{type(exc).__name__}: {exc}"[:240]
                scan["rejections"].append({"inst":inst,
                    "reason":"UNCERTAIN_BROKER_CLOSE",
                    "error":f"{type(exc).__name__}: {exc}"})
                bot.notify(f"CRITICAL {inst}: exit uncertain: {exc}. "
                           "Will reconcile broker position before a future retry.",
                           "BloFin LIVE volume exit safety")
    # Do not trade until any uncertain broker order or closure is reconciled.
    if os.getenv("LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES", "true").strip().lower() == "true":
        scan["status"]="PAUSED_NEW_ENTRIES_KEEP_EXISTING_EXITS"
    elif scan["status"] in ("BLOCK_UNTRACKED_ACCOUNT_POSITIONS","BLOCK_UNCERTAIN_ORDERS"):
        pass
    elif any(p.get("exit_pending") for p in bot.get_tracked_positions(state).values()):
        scan["status"]="WAIT_FOR_EXIT_RECONCILIATION"
    elif not (0<=bot.now_ms()-expected_close_ms<=min(bot.SIGNAL_MAX_AGE_MS,AGE_MAX_MS)):
        scan["status"]="STALE_5M_SCAN"
    elif bot.risk_stop_active(state):
        scan["status"]="BLOCKED_10PCT_DRAWDOWN"

    elif PIVOT_ENABLED:
        # Enter on the exact closed 5m bar confirming pivot point 4,
        # only when the requested larger opposite-colour Volume bar also confirms.
        matches = []
        for rank, inst in enumerate(top200, 1):
            if inst in bot.get_tracked_positions(state) or inst in broker_set:
                continue
            bars = confirmed.get(inst)
            if not bars or int(bot.bar_close_ms(bars[-1])) != expected_close_ms:
                continue
            rows = _by_ts(bars, expected_close_ms)
            tunnel = evaluate_tunnel(
                rows, expected_close_ms, int(pivot_restart.get(inst) or 0))
            if not tunnel["entry_allowed"]:
                continue
            # LONG enters on the second confirmed LOWER pivot.
            # SHORT enters on the second confirmed UPPER pivot.
            side = tunnel["tunnel_direction"]
            entry_anchors = (tunnel.get("lower_anchors") or [] if side == "LONG"
                             else tunnel.get("upper_anchors") or [])
            if (len(entry_anchors) != 2
                    or int(entry_anchors[1].get("confirmed_close_ms") or 0)
                        != int(expected_close_ms)):
                scan["rejections"].append({
                    "inst": inst, "rank": rank, "side": side,
                    "reason": ("WAIT_SECOND_LOWER_BOUNCE" if side == "LONG"
                               else "WAIT_SECOND_UPPER_BOUNCE")})
                continue
            candle = rows.get(expected_close_ms-FIVE)
            if candle is None or candle[4] <= 0:
                continue
            # The two consecutive CLOSED Volume bars must change colour:
            # RED -> larger GREEN for LONG, GREEN -> larger RED for SHORT.
            # Compare candle volumes, not the day's peak or MA20.
            previous = rows.get(expected_close_ms-2*FIVE)
            volume_confirmed = (
                previous is not None
                and candle[4] > previous[4]
                and ((side == "LONG" and previous[3] < previous[0]
                      and candle[3] > candle[0])
                     or (side == "SHORT" and previous[3] > previous[0]
                         and candle[3] < candle[0]))
            )
            if not volume_confirmed:
                scan["rejections"].append({
                    "inst": inst, "rank": rank, "side": side,
                    "reason": "PIVOT_VOLUME_OPPOSITE_BAR_NOT_LARGER",
                    "previous_volume": (previous[4] if previous else None),
                    "signal_volume": candle[4],
                })
                continue
            mean20 = _previous20_mean(rows, expected_close_ms-FIVE)
            strength = candle[4]/mean20 if mean20 and mean20 > 0 else 0.0
            sig = {
                "inst": inst, "rank": rank, "side": side,
                "strategy": "PIVOT_TUNNEL_5M",
                "signal_close_ms": int(expected_close_ms),
                "signal_candle_ts": int(expected_close_ms-FIVE),
                "close": candle[3], "signal_volume": candle[4],
                "volume_strength_20": strength,
                "volume_ratio": strength, "signal_mean20_prev": mean20,
                "previous_opposite_volume": previous[4],
                "volume_flip_confirmed": True,
                "pivot_tunnel_entry": True,
                "second_bounce_entry": True,
                "pivot_tunnel_info": {
                    "upper_anchors": tunnel["upper_anchors"],
                    "lower_anchors": tunnel["lower_anchors"],
                    "last_confirmed_high": tunnel["last_confirmed_high"],
                    "last_confirmed_low": tunnel["last_confirmed_low"],
                    "setup_confirm_close_ms": tunnel["fourth_confirmed_close_ms"],
                },
                "volume_price_tunnel": tunnel,
                **_readonly_entry_indicators(bars, expected_close_ms, side, candle[3]),
            }
            matches.append(sig)
        matches = prioritize_volume_signals(matches)
        scan["selection_priority"] = "PIVOT_FOURTH_CONFIRM_CLOSE_VOLUME_STRENGTH_TIE_RANK"
        scan["entry_volume_trend_filter"] = "PREVIOUS_RED_TO_LARGER_GREEN_OR_GREEN_TO_LARGER_RED"
        scan["matched"] = len(matches)
        scan["candidates"] = matches[:20]
        scan["status"] = "NO_MATCH" if not matches else "MATCHES_NO_ENTRY"
        for sig in matches:
            if scan["status"].startswith("UNCERTAIN"):
                break
            try:
                if _open_one(bot, state, sig, instruments):
                    scan["entries"].append(sig["inst"])
                    scan["status"] = "ORDER_ATTEMPTED"
                    pivot_open[sig["inst"]] = int(sig["signal_close_ms"])
                    pending = state.get("volume_long_order_attempts") or {}
                    if (pending.get(sig["inst"]) or {}).get("status") != "TRACKED":
                        scan["status"] = "ORDER_FILL_UNCERTAIN_BLOCK"
                        break
                elif sig.get("preorder_rejection"):
                    scan["rejections"].append({
                        "inst": sig["inst"], "side": sig["side"],
                        "reason": sig["preorder_rejection"],
                        "pre_order_quote": sig.get("pre_order_quote"),
                    })
            except Exception as exc:
                attempts = state.get("volume_long_order_attempts") or {}
                if (attempts.get(sig["inst"]) or {}).get("status") in (
                    "POST_STARTED", "UNCERTAIN", "BROKER_ACCEPTED", "UNCERTAIN_FILL"):
                    scan["status"] = "UNCERTAIN_ORDER_BLOCK"
                    scan["rejections"].append({
                        "inst": sig["inst"], "reason": "BROKER_ORDER_UNCERTAIN",
                        "error": str(exc)[:240]})
                    bot.notify(f"CRITICAL: {sig['inst']} order uncertain: {exc}",
                               "BloFin LIVE pivot tunnel safety")
                    break
                scan["rejections"].append({
                    "inst": sig["inst"], "reason": "PREORDER_REJECTION",
                    "error": str(exc)[:240]})
    else:
        matches=[]
        for rank,inst in enumerate(top200,1):
            if inst in bot.get_tracked_positions(state):continue
            if inst in broker_set:continue
            bars=confirmed.get(inst)
            if not bars or int(bot.bar_close_ms(bars[-1]))!=expected_close_ms:continue
            sig=entry_signal(bot,bars,expected_close_ms)
            # No 90-100% MA20 signal-volume range gate. Other volume,
            # daily-trend, tunnel and broker-risk rules are unchanged.
            if sig is not None:
                long_rejection = long_entry_extra_rejection(sig, rank)
                if long_rejection:
                    scan["rejections"].append({
                        "inst": inst, "rank": rank, "side": "LONG",
                        "reason": long_rejection,
                    })
                    continue
                trend_data = confirmed_trends.get(inst)
                if trend_data is None:
                    # Targeted backfill for late-day startup only. Do NOT
                    # expand all 200 public requests or guess the trend.
                    scan["volume_trend_backfill_attempts"] += 1
                    try:
                        trend_data = _bootstrap_volume_trend_history(
                            bot, inst, expected_close_ms, volume_trends.get(inst))
                        if trend_data is not None:
                            volume_trends[inst] = trend_data
                            confirmed_trends[inst] = trend_data
                            scan["volume_trend_backfill_successes"] += 1
                    except Exception as exc:
                        scan["rejections"].append({
                            "inst": inst, "rank": rank, "side": sig["side"],
                            "reason": "VOLUME_TREND_HISTORY_UNAVAILABLE",
                            "error": f"{type(exc).__name__}: {exc}"[:220],
                        })
                trend_direction = (trend_data or {}).get("trend", "UNKNOWN")
                if trend_direction != sig["side"]:
                    scan["rejections"].append({
                        "inst": inst, "rank": rank, "side": sig["side"],
                        "reason": "VOLUME_TREND_UNKNOWN"
                                  if trend_direction == "UNKNOWN"
                                  else "VOLUME_TREND_OPPOSES_ENTRY",
                        "volume_trend": trend_direction,
                    })
                    continue
                tunnel = volume_price_tunnel_from_bars(
                    bars, expected_close_ms, sig["side"])
                # A crossover means that the old direction became the NEW
                # direction. Check that interpretation as well instead of
                # rejecting every intersection as invalid data.
                if (tunnel.get("status") == "VALID"
                        and tunnel.get("tunnel_direction") in ("LONG", "SHORT")
                        and (not tunnel.get("entry_allowed")
                             or tunnel["tunnel_direction"] != sig["side"])):
                    old_side = "SHORT" if sig["side"] == "LONG" else "LONG"
                    flipped = volume_price_tunnel_from_bars(
                        bars, expected_close_ms, old_side)
                    if (flipped.get("status") == "VALID"
                            and flipped.get("colour_order_changed")
                            and flipped.get("tunnel_direction") == sig["side"]
                            and flipped.get("entry_allowed")):
                        flipped["reversal_from_side"] = old_side
                        tunnel = flipped
                # Incomplete geometry must fail closed without crashing LIVE.
                # A valid tunnel always states LONG or SHORT explicitly.
                if (tunnel.get("status") != "VALID"
                        or not tunnel.get("entry_allowed")
                        or tunnel.get("tunnel_direction") != sig["side"]):
                    scan["rejections"].append({
                        "inst": inst, "rank": rank, "side": sig["side"],
                        "reason": ("VOLUME_TUNNEL_UNAVAILABLE"
                                   if tunnel.get("status") != "VALID"
                                   or tunnel.get("tunnel_direction") not in ("LONG", "SHORT")
                                   else "VOLUME_TUNNEL_OPPOSITE_TREND"
                                   if tunnel["tunnel_direction"] != sig["side"]
                                   else "PRICE_OUTSIDE_STRAIGHT_TUNNEL"),
                        "tunnel_reason": tunnel.get("reason", "MISSING_TUNNEL_FIELDS"),
                        "volume_tunnel": tunnel,
                    })
                    continue
                sig.update({
                    "inst": inst, "rank": rank,
                    "volume_trend_direction": trend_direction,
                    "volume_trend_peak": trend_data["peak_volume"],
                    "volume_trend_started_close_ms": trend_data["trend_started_close_ms"],
                })
                sig["volume_price_tunnel"] = tunnel
                matches.append(sig)
        # Full TOP200 was already checked against the exact same entry rules.
        # From eligible signals, select strongest relative volume first,
        # regardless of prior 24h price-change ranking.
        matches = prioritize_volume_signals(matches)
        scan["matched"]=len(matches)
        scan["candidates"]=matches[:20]
        scan["status"]="NO_MATCH" if not matches else "MATCHES_NO_ENTRY"
        for sig in matches:
            if scan["status"].startswith("UNCERTAIN"):break
            try:
                if _open_one(bot,state,sig,instruments):
                    scan["entries"].append(sig["inst"])
                    scan["status"]="ORDER_ATTEMPTED"
                    pending=state.get("volume_long_order_attempts") or {}
                    if (pending.get(sig["inst"]) or {}).get("status")!="TRACKED":
                        scan["status"]="ORDER_FILL_UNCERTAIN_BLOCK"
                        break
                elif sig.get("preorder_rejection"):
                    scan["rejections"].append({
                        "inst": sig["inst"], "side": "SHORT",
                        "reason": sig["preorder_rejection"],
                        "pre_order_quote": sig.get("pre_order_quote"),
                    })
            except Exception as exc:
                attempts=state.get("volume_long_order_attempts") or {}
                if (attempts.get(sig["inst"]) or {}).get("status") in (
                        "POST_STARTED","UNCERTAIN","BROKER_ACCEPTED","UNCERTAIN_FILL"):
                    scan["status"]="UNCERTAIN_ORDER_BLOCK"
                    scan["rejections"].append({"inst":sig["inst"],
                        "reason":"BROKER_ORDER_UNCERTAIN","error":str(exc)[:240]})
                    bot.notify(f"CRITICAL: {sig['inst']} order uncertain: {exc}",
                               "BloFin LIVE volume LONG safety")
                    break
                scan["rejections"].append({"inst":sig["inst"],
                    "reason":"PREORDER_REJECTION","error":str(exc)[:240]})
    state.pop("_volume_long_post_started",None)
    state["volume_long_last_scan"]=scan
    state["last_scan_close_ms"]=int(expected_close_ms)
    state["last_run_ms"]=bot.now_ms()
    state["last_top200"]=list(top200)
    bot.save_state(state)
    print(f"{STRATEGY}: {scan['status']} | scanned {scan['scanned_confirmed']}/200 "
          f"| signals {scan['matched']} | entries {len(scan['entries'])} | exits {len(scan['exits'])}")


if __name__=="__main__":
    raise SystemExit("Execute via authorized BloFin LIVE workflow only.")
