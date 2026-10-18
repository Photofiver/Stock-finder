"""LIVE two-red-candle oversold rebound; one 1x LONG at a time.

Reference research: 2026-10-09..18, a 5m RED candle body 0.2%-0.6%,
followed by a CLOSED RED candle body 0.4%-<0.8% whose close is at least
2% below SMA10. Buy following candle only. TP+1.5%, SL-1%, max hold 60m.
Scan ranking: current BloFin ticker 24h gainers TOP200; this is an
approximation of the research's candle-to-candle rolling 24h gain ranking.

Cost model used in RESEARCH was 0.22% round trip; LIVE execution uses
exchange fees and actual market spreads and is not guaranteed to match.
Never open >20 USDT notional; require >=25 USDT FREE before opening,
1x isolated, existing 10%-of-bankroll hard stop, one account position total.
Native broker TP+SL protects between scans; time exit checked at each 5m
scheduled run (can be delayed by GitHub Actions).
Old positions are NEVER modified to the new TP/SL.
"""
import math
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

STRATEGY = "RED_02_06_THEN_RED_04_08_SMA10_MINUS2_LONG_V1"
HOLD_POLICY = "HOLD_60M"
NOTIONAL_USDT = Decimal("20")
MIN_FREE_USDT = Decimal("25")
TP_PCT = Decimal("0.015")
SL_PCT = Decimal("0.01")
HOLD_MS = 60 * 60 * 1000
AGE_MAX_MS = 120_000
CANDLE_MS = 5 * 60 * 1000
PREVIOUS_BODY_MIN = 0.2
PREVIOUS_BODY_MAX_INCLUSIVE = 0.6
SIGNAL_BODY_MIN = 0.4
SIGNAL_BODY_MAX_EXCLUSIVE = 0.8
SMA10_MIN_BELOW_PCT = 2.0


def closed_candle_signal(bot, bars, expected_close_ms):
    """Evaluate ONLY the 10 latest *confirmed* candles; no future OHLCV."""
    if len(bars) < 10:
        return None
    bars = sorted(bars, key=lambda x: int(x["ts"]))
    latest = bars[-1]
    previous = bars[-2]
    if (bot.bar_close_ms(latest) != expected_close_ms
            or bot.bar_close_ms(previous) != expected_close_ms - CANDLE_MS):
        return None
    # Guard against API holes, NaN, zero closes and an incomplete signal bar.
    block = bars[-10:]
    for i, candle in enumerate(block):
        if int(candle["ts"]) != int(latest["ts"]) - (9-i)*CANDLE_MS:
            return None
        if any(not math.isfinite(float(candle[k])) for k in ("o","h","l","c","v")):
            return None
        if float(candle["o"]) <= 0 or float(candle["c"]) <= 0 or float(candle["v"]) < 0:
            return None
    prev_o, prev_c = float(previous["o"]), float(previous["c"])
    o, c = float(latest["o"]), float(latest["c"])
    prev_body = 100.0 * (1.0 - prev_c / prev_o)
    signal_body = 100.0 * (1.0 - c / o)
    if not (PREVIOUS_BODY_MIN <= prev_body <= PREVIOUS_BODY_MAX_INCLUSIVE):
        return None
    if not (SIGNAL_BODY_MIN <= signal_body < SIGNAL_BODY_MAX_EXCLUSIVE):
        return None
    sma10 = sum(float(x["c"]) for x in block) / 10.0
    if sma10 <= 0 or 100.0 * (c / sma10 - 1.0) > -SMA10_MIN_BELOW_PCT:
        return None
    return {
        "side": "LONG",
        "strategy": STRATEGY,
        "signal_close_ms": expected_close_ms,
        "close": c,
        "previous_red_body_pct": round(prev_body, 5),
        "signal_red_body_pct": round(signal_body, 5),
        "sma10_at_signal": sma10,
        "signal_close_below_sma10_pct": round(100.0*(1-c/sma10), 5),
        "previous_candle_ts": int(previous["ts"]),
        "signal_candle_ts": int(latest["ts"]),
    }


def broker_targets(bot, filled_price, tick):
    entry = bot.d(filled_price)
    tick_dec = bot.d(tick)
    if not (entry.is_finite() and tick_dec.is_finite() and entry > 0 and tick_dec > 0):
        raise ValueError("Invalid entry price or tick")
    tp = bot.price_step(entry*(Decimal("1")+TP_PCT), tick_dec, ROUND_CEILING)
    # For a LONG, round stop upward to keep realized trigger no worse than -1%.
    sl = bot.price_step(entry*(Decimal("1")-SL_PCT), tick_dec, ROUND_CEILING)
    if not (sl < entry < tp):
        raise ValueError(f"Tick size prevents valid TP/SL on {entry}: {sl}, {tp}")
    return tp, sl


def open_long(bot, state, signal, rank, instruments):
    inst = signal["inst"]
    if bot.risk_stop_active(state):
        return False
    if bot.get_tracked_positions(state) or bot.get_open_positions():
        return False
    if inst not in instruments:
        return False
    age = bot.now_ms() - int(signal["signal_close_ms"])
    if not (0 <= age <= min(AGE_MAX_MS, bot.SIGNAL_MAX_AGE_MS)):
        print(f"{STRATEGY} STALE {inst} candle age {age}ms")
        return False

    # This research assumed 25 USDT capital with a constant 20 USDT 1x position.
    # Do not silently replace a smaller real balance with an invented balance.
    available = bot.get_available_usdt()
    bankroll = bot.current_live_bankroll(state)
    if available < MIN_FREE_USDT or bankroll < NOTIONAL_USDT:
        state["red_sma_last_block"] = {
            "reason": "INSUFFICIENT_CAPITAL",
            "required_available_usdt": str(MIN_FREE_USDT),
            "available_usdt": bot.clean_decimal(available),
            "bankroll_usdt": bot.clean_decimal(bankroll),
        }
        print(f"{STRATEGY}: need >=25 free USDT and >=20 bankroll; "
              f"available={available} bankroll={bankroll}")
        return False
    if bot.MAX_NOTIONAL_USDT < NOTIONAL_USDT:
        print(f"{STRATEGY}: configured hard cap {bot.MAX_NOTIONAL_USDT} < 20 USDT")
        return False

    # Market executable ASK (not last) must be fresh; do not chase a rebound
    # more than shared 0.25% drift or trade an excessive spread.
    quote = bot.checked_preorder_quote(
        inst, "LONG", signal["close"], enforce_adverse_gap=True,
    )
    sized = bot.size_for_notional(bot.d(quote["expected_fill"]),
                                  instruments[inst], NOTIONAL_USDT)
    if not sized:
        print(f"{STRATEGY}: {inst} contract min/step incompatible with 20 USDT")
        return False
    size, planned_notional = sized
    # The live candle setup was validated without orderbook imbalance gating;
    # only spread, freshness, position/size/capital and broker protection gate.
    tick = bot.d(instruments[inst].get("tickSize") or "0")
    try:
        _ = broker_targets(bot, quote["expected_fill"], tick)
    except (TypeError, ValueError, ArithmeticError) as exc:
        print(f"{STRATEGY}: {inst} invalid TP/SL tick or reference price: {exc}")
        return False

    bot.set_one_x(inst)
    client_id = ("livered" + uuid.uuid4().hex)[:32]
    state["red_sma_order_attempt"] = {
        "client_order_id": client_id,
        "inst": inst, "signal_close_ms": signal["signal_close_ms"],
        "status": "POST_STARTED", "started_ms": bot.now_ms(),
    }
    # Persist before POST in this runner; the exception handler keeps uncertain
    # status when the API response is lost. Prevent second order on same scan.
    bot.save_state(state)
    state["_red_sma_order_post_started"] = True
    result = bot.private_request(
        "POST", "/api/v1/trade/order",
        body={
            "instId": inst,
            "marginMode": bot.MARGIN_MODE,
            "positionSide": "net",
            "side": "buy",
            "orderType": "market",
            "size": bot.clean_decimal(size),
            "reduceOnly": "false",
            "clientOrderId": client_id,
        },
    )
    row = result[0] if isinstance(result,list) and result else (result or {})
    if str(row.get("code", "0")) != "0":
        raise RuntimeError(f"BloFin entry rejected: {row}")
    opened_ms = bot.now_ms()
    pos = {
        "inst": inst, "side": "LONG", "opened_ms": opened_ms,
        "signal_close_ms": int(signal["signal_close_ms"]),
        "signal_age_ms": age, "signal_rank": int(rank),
        "signal_snapshot": dict(signal, pre_order_quote=quote),
        "order_id": str(row.get("orderId") or ""),
        "client_order_id": client_id,
        "reference_entry": "", "filled_size": "", "size": bot.clean_decimal(size),
        "notional_usdt": bot.clean_decimal(planned_notional),
        "tp": "", "sl": "",
        "tp_policy": "TP_GROSS1P5",
        "sl_policy": "SL_GROSS1_BROKER",
        "hold_policy": HOLD_POLICY,  # Shared existing 60m exit; NEVER cancel broker TP/SL.
        "strategy": STRATEGY,
        "code_commit": bot.CODE_COMMIT,
        "risk_profile": "ONE_POSITION_1X_TP15_SL1_TIME60",
        "protection_status": "BROKER_TPSL_PENDING",
        "allocation_label": "25 USDT capital, 20 USDT 1x fixed notional",
    }
    bot.get_tracked_positions(state)[inst] = pos
    state["red_sma_order_attempt"]["status"] = "ACCEPTED_BROKER"
    bot.save_state(state)
    fill = bot.wait_for_order_fill(inst, pos["order_id"], client_id)
    if not fill:
        bot.notify(
            f"CRITICAL {inst}: accepted order but fill not verified. Requesting safe close.",
            "BloFin LIVE SAFETY",
        )
        bot._run_for_tracked_position(
            state, inst, bot.close_tracked_position, "SAFETY: unverified rebound fill",
        )
        return True
    actual_price = bot.d(fill.get("averagePrice") or 0)
    filled_size = bot.d(fill.get("filledSize") or 0)
    if actual_price <= 0 or filled_size <= 0:
        bot.notify(f"CRITICAL {inst}: invalid fill after market order", "BloFin LIVE SAFETY")
        bot._run_for_tracked_position(
            state, inst, bot.close_tracked_position, "SAFETY: invalid rebound fill",
        )
        return True
    contract_value = bot.d(instruments[inst].get("contractValue") or 0)
    actual_notional = actual_price*filled_size*contract_value
    pos.update({
        "reference_entry": bot.clean_decimal(actual_price),
        "filled_size": bot.clean_decimal(filled_size),
        "notional_usdt": bot.clean_decimal(actual_notional),
        "entry_fee": str(fill.get("fee") or ""),
    })
    try:
        tp, sl = broker_targets(bot, actual_price, tick)
        tpsl_id, tpsl_client_id = bot.place_tpsl_for_position(inst, "LONG", tp, sl)
        if not tpsl_id:
            raise RuntimeError("Broker returned no TP/SL ID")
        pos.update({
            "tp": bot.clean_decimal(tp), "sl": bot.clean_decimal(sl),
            "tpsl_id": tpsl_id, "tpsl_client_order_id": tpsl_client_id,
            "protection_status": "BROKER_TPSL_ACTIVE",
        })
        bot.save_state(state)
    except Exception as exc:
        pos["protection_status"] = "CRITICAL_TPSL_FAILED_CLOSE_REQUESTED"
        pos["protection_error"] = str(exc)
        bot.notify(
            f"CRITICAL {inst}: could not set broker TP+1.5% SL-1%; closing: {exc}",
            "BloFin LIVE SAFETY",
        )
        try:
            bot._run_for_tracked_position(
                state, inst, bot.close_tracked_position, "SAFETY: rebound TP/SL failed",
            )
        except Exception as close_error:
            bot.notify(
                f"CRITICAL {inst}: safety close failed: {close_error}",
                "BloFin LIVE SAFETY",
            )
        return True

    # Keep diagnostics OUTSIDE broker protection critical path.
    try:
        from blofin_trade_charts import capture_chart
        pos["entry_chart"] = capture_chart(
            bot, pos, "ENTRY", opened_ms, actual_price,
        )
    except Exception as exc:
        pos["entry_chart_error"] = f"{type(exc).__name__}: {exc}"
    bot.notify(
        f"OPEN LONG {inst} | red(0.2-0.6%) -> red(0.4-0.8%) "
        f"| close >=2% under SMA10 | rank {rank}/200 | 1x "
        f"| notional {pos['notional_usdt']} USDT | broker TP +1.5% {pos['tp']} "
        f"SL -1% {pos['sl']} | max 60min (checked on 5m scan)",
        "BloFin LIVE RED SMA10 REBOUND",
        attachment_path=pos.get("entry_chart"),
    )
    state["red_sma_order_attempt"]["status"] = "PROTECTED"
    return True


def run(bot, state, top200, tickers, instruments, expected_close_ms,
        wait_for_confirmed_close):
    if bot.SIGNAL_MINUTES != 5:
        raise RuntimeError("RED SMA10 REBOUND requires confirmed 5m candles")
    if bot.TOP_N != 200 or len(top200) != 200:
        raise RuntimeError("RED SMA10 REBOUND requires exact TOP200 rank list")
    if int(state.get("last_scan_close_ms") or 0) == expected_close_ms:
        print(f"{STRATEGY}: duplicate scan skipped")
        return

    last = {
        "strategy": STRATEGY, "signal_close_ms": expected_close_ms,
        "rank_basis": "current_BloFin_ticker_24h_last_over_open24h",
        "status": "PENDING", "top200_count":len(top200),
        "scanned_confirmed":0,"matched":0,"candidates":[],
    }
    # This migration leaves open orders and their original TP/SL UNCHANGED.
    # Reconcile the broker first, enforce existing 60m holds, then require a
    # completely flat account. If an old SMA10 trailing position remains,
    # continue its original 10m monitor instead of silently abandoning safety.
    broker_open_before = bot.get_open_positions()
    tracked_before = bot.get_tracked_positions(state)
    bot.sync_all_tracked_positions(state)
    bot.evaluate_all_tracked_exit_signals(state, expected_close_ms)
    active = bot.get_tracked_positions(state)
    if active or bot.get_open_positions():
        last["status"] = "WAIT_EXISTING_POSITION"
        last["positions"] = [
            {"inst":inst,"strategy":pos.get("strategy"),
             "hold_policy":pos.get("hold_policy")} for inst,pos in active.items()
        ]
        # New trades remain blocked until the old strategy finishes.
        if active:
            try:
                import blofin_ma10_ma20_live as ma_cross
                for inst,pos in list(active.items()):
                    if pos.get("hold_policy")==ma_cross.DYNAMIC_MA10_HOLD_POLICY:
                        ma_cross.enforce_max_ten_minutes(
                            bot, state, inst, instruments,
                        )
            except Exception as exc:
                print(f"OLD_SMA_STOP_MONITOR_WARNING: {type(exc).__name__}: {exc}")
        state["red_sma_last_scan"] = last
        state["last_scan_close_ms"] = expected_close_ms
        state["last_run_ms"] = bot.now_ms()
        bot.save_state(state)
        return

    # Permanent safety latch after a potentially uncertain market POST.
    # Do not open more positions unless this is explicitly reconciled.
    attempt=state.get("red_sma_order_attempt") or {}
    if attempt.get("status") in ("POST_STARTED","UNCERTAIN","ACCEPTED_BROKER"):
        last["status"]="UNCERTAIN_PREVIOUS_ORDER_BLOCK"
        last["attempt"]=attempt
        state["red_sma_last_scan"]=last
        state["last_scan_close_ms"]=expected_close_ms
        bot.save_state(state)
        return

    if bot.risk_stop_active(state):
        last["status"]="BLOCKED_DRAWDOWN_RISK_STOP"
    elif not (0 <= bot.now_ms()-expected_close_ms<=min(bot.SIGNAL_MAX_AGE_MS,AGE_MAX_MS)):
        last["status"]="STALE_5M_SCAN"
    else:
        # Confirm all 200 already-ranked tickers. No partial candle lookahead,
        # and one globally-ranked entry at most (backtest tie break).
        confirmed = wait_for_confirmed_close(top200,expected_close_ms) or {}
        last["scanned_confirmed"]=len(confirmed)
        matches=[]
        for rank,inst in enumerate(top200,start=1):
            bars=confirmed.get(inst)
            if not bars:continue
            try:
                sig=closed_candle_signal(bot,bars,expected_close_ms)
                if sig:
                    sig["inst"]=inst;sig["rank"]=rank
                    matches.append(sig)
            except (ValueError,KeyError,TypeError,OverflowError) as exc:
                print(f"{STRATEGY}: candle validation {inst}: {exc}")
        last["matched"]=len(matches)
        last["candidates"]=matches[:20]
        last["status"]="NO_MATCH" if not matches else "MATCHES_NO_TRADE"
        for sig in matches:
            if state.get("_red_sma_order_post_started"):break
            if bot.risk_stop_active(state) or bot.get_tracked_positions(state) or bot.get_open_positions():
                last["status"]="BLOCKED_POSITION_OR_RISK"
                break
            try:
                if open_long(bot,state,sig,sig["rank"],instruments):
                    last["status"]="ORDER_ATTEMPTED"
                    last["selected"]=sig
                    break
            except Exception as exc:
                if state.get("_red_sma_order_post_started"):
                    attempt=state.setdefault("red_sma_order_attempt",{})
                    attempt["status"]="UNCERTAIN"
                    last["status"]="ORDER_UNCERTAIN_BLOCK_FUTURE"
                    last["error"]=f"{type(exc).__name__}: {exc}"
                    bot.notify(
                        f"CRITICAL {sig['inst']}: market order result uncertain: {exc}",
                        "BloFin LIVE RED SMA SAFETY",
                    )
                    break
                last["status"]="PREORDER_REJECTED_TRY_NEXT"
                last.setdefault("rejections",[]).append({
                    "inst":sig["inst"],"rank":sig["rank"],
                    "error":f"{type(exc).__name__}: {exc}",
                })
    state.pop("_red_sma_order_post_started",None)
    state["red_sma_last_scan"]=last
    state["last_scan_close_ms"]=expected_close_ms
    state["last_run_ms"]=bot.now_ms()
    state["last_top200"]=top200
    bot.save_state(state)
    print(f"{STRATEGY}: {last['status']} "
          f"| confirmed {last['scanned_confirmed']}/200 "
          f"| qualifying {last['matched']}")


if __name__=="__main__":
    raise SystemExit("Run through blofin_live_signal_watch.py; direct orders disabled.")
