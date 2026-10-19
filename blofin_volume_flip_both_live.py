"""LIVE both-sided volume flip on CLOSED 5m BloFin candles.

LONG: qualified GREEN after qualified RED, green volume > prior red.
SHORT: qualified RED after qualified GREEN, red volume > prior green.
Qualified means each candle volume <= its own prior 20-bar mean.
Exit on any CLOSED opposite-color candle volume > the OPENING signal
candle volume, WITHOUT a volume-average filter; next 5m scan.

No BTC/price/DI/3-4x filters. Broker TP +0.55%, SL -0.50% for both sides.
Existing legacy red-volume LONG positions retain their original exit.
1x isolated, up to four different coins, shared bankroll and risk gate.
"""
import math
import os
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

STRATEGY = "VOLUME_FLIP_BOTH_5M_V1"
LEGACY_STRATEGY = "REV_SHORT_20W_LONG_VOLUME_V1"
HOLD_POLICY = "VOLUME_FLIP_BOTH_NO_TPSL"
TP_PCT = Decimal("0.0055")
SL_PCT = Decimal("0.005")
TP_POLICY = "TP055"
SL_POLICY = "SL05"
TRIGGER_TYPE = "mark"  # Match unrealized PnL displayed by BloFin.
ENTRY_VOLUME_STRENGTH_MIN = 0.90  # inclusive; volume / prior 20-bar mean
ENTRY_VOLUME_STRENGTH_MAX = 1.00  # exclusive; do not enter at or above mean
LEGACY_HOLD_POLICY = "VOLUME_REV_SHORT_LONG"
FIVE = 300_000
VOL_MIN = 3.0
VOL_MAX_EXCLUSIVE = 4.0
BTC15_MAX = 0.062055813
COIN15_MIN = -0.10322961
MAX_POSITIONS = 4  # additional to per-coin uniqueness; existing account cap
MAX_PER_COIN_USDT = Decimal("20")
MIN_PER_COIN_USDT = Decimal("5")
CASH_BUFFER_USDT = Decimal("0.50")
AGE_MAX_MS = 120_000



def _protection_prices(bot, pos, meta):
    """Tick-aligned triggers from actual broker fill; do not exceed -0.50% SL."""
    entry=bot.d(pos.get("reference_entry") or "0")
    tick=bot.d((meta or {}).get("tickSize") or "0")
    if entry<=0 or tick<=0 or not entry.is_finite() or not tick.is_finite():
        raise RuntimeError("Verified entry and tickSize are required for TP/SL")
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
    """Verify broker OCO and safely amend existing LAST triggers to MARK."""
    inst=pos["inst"]
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
        if types==(TRIGGER_TYPE,TRIGGER_TYPE):
            pos.update(tp_trigger_type=TRIGGER_TYPE,sl_trigger_type=TRIGGER_TYPE,
                       protection_status="TP055_SL05_MARK_ACTIVE")
            pos.pop("mark_amend_request_id",None)
            pos.pop("protection_error",None)
            bot.save_state(state)
            return True
        if types!=("last","last"):
            raise RuntimeError("Unexpected broker TP/SL trigger types: "+str(types))
        if pos.get("mark_amend_request_id"):
            raise RuntimeError("Prior mark-trigger amendment is awaiting broker confirmation")
        request_id=("volmark"+uuid.uuid4().hex)[:32]
        pos["mark_amend_request_id"]=request_id
        pos["protection_status"]="MARK_TPSL_AMEND_SENT"
        bot.save_state(state)
        result=bot.private_request("POST","/api/v1/trade/amend-tpsl",body={
            "instId":inst,"tpslId":str(pos["tpsl_id"]),"requestId":request_id,
            "newTpTriggerPriceType":TRIGGER_TYPE,"newSlTriggerPriceType":TRIGGER_TYPE
        })
        row=result[0] if isinstance(result,list) and result else (result or {})
        if str(row.get("code","0"))!="0":
            raise RuntimeError("Broker rejected trigger amendment: "+str(row)[:180])
        # Broker acknowledgement alone is insufficient; verify on next read.
        return _ensure_mark_trigger(bot,state,pos,tp,sl)
    except Exception as exc:
        pos["protection_status"]="TPSL_MARK_VERIFICATION_PENDING"
        pos["protection_error"]=f"{type(exc).__name__}: {exc}"[:250]
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
                           sl=bot.clean_decimal(sl),tp_policy=TP_POLICY,
                           sl_policy=SL_POLICY)
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
               tp_policy=TP_POLICY,sl_policy=SL_POLICY)
    bot.save_state(state)
    try:
        response=bot.private_request("POST","/api/v1/trade/order-tpsl",body={
            "instId":inst,"marginMode":bot.MARGIN_MODE,"positionSide":"net",
            "side":"sell" if pos["side"]=="LONG" else "buy",
            "tpTriggerPrice":bot.clean_decimal(tp),"tpOrderPrice":"-1",
            "tpTriggerPriceType":TRIGGER_TYPE,"slTriggerPrice":bot.clean_decimal(sl),
            "slOrderPrice":"-1","slTriggerPriceType":TRIGGER_TYPE,
            "size":"-1","reduceOnly":"true","clientOrderId":client,
        })
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
    }


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


def _open_one(bot, state, sig, instruments):
    inst = sig["inst"]
    side = sig["side"]
    if side not in ("LONG","SHORT"):
        return False
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
    # same 25 USDT bankroll anew for every independent symbol.
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

    # Split remaining capital across at least two slots when feasible.
    target = min(MAX_PER_COIN_USDT, spendable/Decimal("2"))
    if target < MIN_PER_COIN_USDT:
        target = min(MAX_PER_COIN_USDT, spendable)
    quote = bot.checked_preorder_quote(inst, side, sig["close"],
                                      enforce_adverse_gap=True)
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
        "strategy":STRATEGY,"hold_policy":HOLD_POLICY,
        "last_same_volume":sig["signal_volume"],
        "last_checked_signal_close_ms":int(sig["signal_close_ms"]),
        "order_id":str(row.get("orderId") or ""),
        "client_order_id":order_id,
        "size":bot.clean_decimal(size),
        "reference_entry":"",
        "filled_size":"",
        "notional_usdt":bot.clean_decimal(expected_notional),
        "tp":"","sl":"","tp_policy":"NONE","sl_policy":"NONE",
        "risk_profile":"1X_VOLUME_FLIP_TP055_SL05",
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
    if not _ensure_protection(bot,state,pos,meta):
        attempt["status"]="PROTECTION_UNCERTAIN"
        bot.save_state(state)
        bot.notify(f"CRITICAL {side} {inst}: TP +0.55% / SL -0.50% "
                   "not verified at BloFin; no additional entries until confirmed.",
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
        f"TP +0.55% {pos['tp']} (Mark) | SL -0.50% {pos['sl']} (Mark) | "
        "EXIT: broker TP/SL or opposite CLOSED 5m volume > ENTRY candle volume.",
        "BloFin LIVE both sides volume flip",
        attachment_path=pos.get("entry_chart"),
    )
    return True



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
          "volume_entry_strength_min_inclusive":ENTRY_VOLUME_STRENGTH_MIN,
          "volume_entry_strength_max_exclusive":ENTRY_VOLUME_STRENGTH_MAX,
          "signal_close_ms":int(expected_close_ms),
          "top200_count":len(top200),"status":"PENDING",
          "scanned_confirmed":0,"matched":0,"candidates":[],
          "entries":[],"exits":[],"rejections":[]}

    bot.sync_all_tracked_positions(state)
    bot.evaluate_all_tracked_exit_signals(state, expected_close_ms)

    active = bot.get_tracked_positions(state)
    broker = bot.get_open_positions()
    broker_set={str(p.get("instId")) for p in broker}
    unprotected=[]
    # Upgrade already-open V1 positions using their actual filled entry price.
    for inst,pos in list(active.items()):
        if (inst in broker_set and pos.get("strategy")==STRATEGY
                and not pos.get("exit_pending")):
            try:
                protected=_ensure_protection(bot,state,pos,instruments.get(inst))
            except Exception as exc:
                protected=False
                pos["protection_error"]=f"{type(exc).__name__}: {exc}"[:250]
                bot.save_state(state)
            if protected:
                attempt=(state.get("volume_long_order_attempts") or {}).get(inst)
                if attempt and attempt.get("status") in ("PROTECTING","PROTECTION_UNCERTAIN"):
                    attempt["status"]="TRACKED"
                    bot.save_state(state)
            else:
                unprotected.append(inst)
                bot.notify(f"CRITICAL {inst}: TP/SL 0.55/0.50 not verified; "
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
    # Archive all confirmed 5m candles for tracked trades, including trades
    # waiting for broker close reconciliation. No new BloFin API calls.
    for inst, pos in list(active.items()):
        bars = confirmed.get(inst)
        if (pos.get("strategy")==STRATEGY and bars and
                int(bot.bar_close_ms(bars[-1]))==expected_close_ms):
            _audit_confirmed_volume_candles(pos, bars, expected_close_ms)
    for inst,pos in list(active.items()):
        legacy = pos.get("strategy")==LEGACY_STRATEGY and pos.get("hold_policy")==LEGACY_HOLD_POLICY
        if not legacy and not (pos.get("strategy")==STRATEGY and pos.get("hold_policy")==HOLD_POLICY):
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
        should_close,new_volume=(legacy_exit_transition(pos,bars,expected_close_ms)
                                 if legacy else exit_transition(pos,bars,expected_close_ms))
        pos["last_red_volume" if legacy else "last_same_volume"]=new_volume
        pos["last_checked_signal_close_ms"]=int(expected_close_ms)
        if should_close:
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
                    ("LEGACY VOLUME: GREEN vol > last RED" if legacy else
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
    else:
        matches=[]
        for rank,inst in enumerate(top200,1):
            if inst in bot.get_tracked_positions(state):continue
            if inst in broker_set:continue
            bars=confirmed.get(inst)
            if not bars or int(bot.bar_close_ms(bars[-1]))!=expected_close_ms:continue
            sig=entry_signal(bot,bars,expected_close_ms)
            # Entry-only filter: 90% <= signal 5m volume / PRIOR 20-bar
            # average < 100%. Does not alter exits or protective TP/SL.
            if (sig is not None and
                    ENTRY_VOLUME_STRENGTH_MIN <= sig["volume_strength_20"]
                    < ENTRY_VOLUME_STRENGTH_MAX):
                sig.update({"inst":inst,"rank":rank})
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
