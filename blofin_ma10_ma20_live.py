"""LIVE SMA10/SMA20 price crossover strategy for 10m BloFin TOP7.

New SMA positions use broker take-profit +1% and stop-loss -1% PRICE (before fees).
Exit also on opposite RSI(14)/SMA(14) cross or opposite price SMA10/SMA20 cross
on a confirmed 10m candle. Indicator exits are software-driven.
Pre-existing SMA positions receive broker SL protection on the next scan.
Legacy non-SMA positions retain their original protection until naturally closed.
Only one account position is permitted while this strategy is active.
"""
import uuid


PRICE_TAKE_PROFIT_PCT = 0.01
PRICE_STOP_LOSS_PCT = 0.01
RSI_AVERAGE_PERIOD = 14


def gross_one_percent_tp(bot, side, entry, tick):
    """Set the broker TP at a 1% price move before fees (not 1% NET)."""
    price = bot.d(entry)
    target = bot.d(PRICE_TAKE_PROFIT_PCT)
    if price <= 0:
        raise ValueError("TP: invalid entry price")
    if side == "LONG":
        return bot.price_step(price * (1 + target), tick, bot.ROUND_CEILING)
    if side == "SHORT":
        return bot.price_step(price * (1 - target), tick, bot.ROUND_FLOOR)
    raise ValueError("TP: invalid side")


def gross_one_percent_sl(bot, side, entry, tick):
    """Broker SL at about -1% unfavorable PRICE move, rounded protectively."""
    price = bot.d(entry)
    amount = bot.d(PRICE_STOP_LOSS_PCT)
    if price <= 0:
        raise ValueError("SL: invalid entry price")
    if side == "LONG":
        return bot.price_step(price * (1 - amount), tick, bot.ROUND_CEILING)
    if side == "SHORT":
        return bot.price_step(price * (1 + amount), tick, bot.ROUND_FLOOR)
    raise ValueError("SL: invalid side")


def crossover(bot, inst, expected_close_ms, bars=None):
    bars = bot.fetch_signal_bars(inst) if bars is None else bars
    if len(bars) < 21 or bot.bar_close_ms(bars[-1]) != expected_close_ms:
        raise RuntimeError(f"{inst}: expected confirmed 10m close unavailable")
    closes = [float(bar["c"]) for bar in bars]
    old_fast = sum(closes[-11:-1]) / 10
    old_slow = sum(closes[-21:-1]) / 20
    new_fast = sum(closes[-10:]) / 10
    new_slow = sum(closes[-20:]) / 20
    side = None
    if old_fast <= old_slow and new_fast > new_slow:
        side = "LONG"
    elif old_fast >= old_slow and new_fast < new_slow:
        side = "SHORT"
    return {
        "inst": inst,
        "side": side,
        "ma10_previous": old_fast,
        "ma20_previous": old_slow,
        "ma10": new_fast,
        "ma20": new_slow,
        "close": closes[-1],
        "signal_close_ms": expected_close_ms,
    }


def rsi_average_crossover(bot, inst, expected_close_ms, bars=None):
    """Cross of RSI(14) against SMA(14) of RSI, confirmed at the 10m close."""
    bars = bot.fetch_signal_bars(inst) if bars is None else bars
    if bot.bar_close_ms(bars[-1]) != expected_close_ms:
        raise RuntimeError(f"{inst}: expected confirmed 10m RSI close unavailable")
    lookback = RSI_AVERAGE_PERIOD + 1
    if len(bars) < bot.RSI_PERIOD + lookback:
        raise RuntimeError(f"{inst}: not enough RSI history for moving average")
    values = [
        float(bar["rsi"]) if bar.get("rsi") is not None else None
        for bar in bars[-lookback:]
    ]
    if any(value is None for value in values):
        raise RuntimeError(f"{inst}: incomplete RSI values at confirmed close")
    prev_rsi, current_rsi = values[-2], values[-1]
    prev_average = sum(values[:-1]) / RSI_AVERAGE_PERIOD
    current_average = sum(values[1:]) / RSI_AVERAGE_PERIOD
    direction = None
    if prev_rsi <= prev_average and current_rsi > current_average:
        direction = "LONG"
    elif prev_rsi >= prev_average and current_rsi < current_average:
        direction = "SHORT"
    return {
        "inst": inst,
        "signal_close_ms": expected_close_ms,
        "rsi14_previous": prev_rsi,
        "rsi14": current_rsi,
        "rsi_sma14_previous": prev_average,
        "rsi_sma14": current_average,
        "cross": direction,
    }


def open_position(bot, state, signal, rank, instruments):
    inst, side = signal["inst"], signal["side"]
    if side not in ("LONG", "SHORT"):
        return False
    if bot.risk_stop_active(state):
        print("SMA CROSS entry blocked by existing 10% bankroll drawdown limit")
        return False
    if bot.get_tracked_positions(state) or bot.get_open_positions():
        print("SMA CROSS entry blocked: another position exists on the account")
        return False
    age_ms = bot.now_ms() - signal["signal_close_ms"]
    if not (0 <= age_ms <= bot.SIGNAL_MAX_AGE_MS):
        print(f"SMA CROSS {inst}: stale cross, age={age_ms}ms")
        return False
    if inst not in instruments:
        print(f"SMA CROSS {inst}: missing instrument metadata")
        return False
    # No entry gate based on signal candle close: accept directional movement.
    # Fresh quotes and bid/ask spread checks still apply.
    quote = bot.checked_preorder_quote(inst, side, signal["close"], enforce_adverse_gap=False)
    cap = min(bot.MAX_NOTIONAL_USDT, bot.current_live_bankroll(state), bot.get_available_usdt())
    if cap <= 0:
        print("SMA CROSS entry blocked: no bankroll")
        return False
    sized = bot.size_for_notional(bot.d(quote["expected_fill"]), instruments[inst], cap)
    if sized is None:
        print(f"SMA CROSS {inst}: minimum lot exceeds bankroll {cap} USDT")
        return False
    size, estimated_notional = sized
    bot.set_one_x(inst)
    client_id = ("livema" + uuid.uuid4().hex)[:32]
    state["_sma_order_post_started"] = True
    order = bot.private_request(
        "POST", "/api/v1/trade/order",
        body={
            "instId": inst,
            "marginMode": bot.MARGIN_MODE,
            "positionSide": "net",
            "side": "buy" if side == "LONG" else "sell",
            "orderType": "market",
            "size": bot.clean_decimal(size),
            "reduceOnly": "false",
            "clientOrderId": client_id,
        },
    )
    row = order[0] if isinstance(order, list) and order else (order or {})
    if str(row.get("code", "0")) != "0":
        raise RuntimeError(f"SMA CROSS broker order rejected: {row}")
    opened_ms = bot.now_ms()
    position = {
        "inst": inst,
        "side": side,
        "opened_ms": opened_ms,
        "signal_close_ms": signal["signal_close_ms"],
        "signal_age_ms": age_ms,
        "signal_rank": rank,
        "signal_snapshot": dict(signal, pre_order_price_guard=quote),
        "order_id": str(row.get("orderId") or ""),
        "client_order_id": client_id,
        "reference_entry": "",
        "filled_size": "",
        "size": bot.clean_decimal(size),
        "notional_usdt": bot.clean_decimal(estimated_notional),
        "tp": "",
        "sl": "",
        "tp_policy": "TP_GROSS1",
        "sl_policy": "SL_GROSS1_BROKER",
        "hold_policy": "SMA10_SMA20_OPPOSITE_CROSS",
        "protection_status": "SMA_PRICE1_TP_SL_PENDING",
        "strategy": f"SMA10_SMA20_{side}_10m",
        "code_commit": bot.CODE_COMMIT,
        "risk_profile": "ONE_POSITION_1X_TP_GROSS1_SL_GROSS1_RSI_CROSS_EXIT",
        "account_fraction": "",
        "allocation_label": f"SMA CROSS <= {bot.clean_decimal(cap)} USDT bankroll",
    }
    # Record immediately after the exchange accepted the market order.
    # An uncertain POST must never trigger an alternative order on this scan.
    bot.get_tracked_positions(state)[inst] = position
    fill = bot.wait_for_order_fill(inst, position["order_id"], client_id)
    if not fill:
        bot.notify(f"SMA CROSS {inst}: market order accepted but fill unverified; safety close requested", "BloFin LIVE SAFETY")
        bot._run_for_tracked_position(state, inst, bot.close_tracked_position, "SAFETY: unverified SMA fill")
        return False
    actual_price = bot.d(fill.get("averagePrice") or "0")
    filled_size = bot.d(fill.get("filledSize") or "0")
    if actual_price <= 0 or filled_size <= 0:
        bot.notify(f"SMA CROSS {inst}: invalid fill, requesting safety close", "BloFin LIVE SAFETY")
        bot._run_for_tracked_position(state, inst, bot.close_tracked_position, "SAFETY: invalid SMA fill")
        return False
    contract_value = bot.d(instruments[inst].get("contractValue") or "0")
    actual_notional = filled_size * contract_value * actual_price
    entry_fee = fill.get("fee")
    position.update({
        "reference_entry": bot.clean_decimal(actual_price),
        "filled_size": bot.clean_decimal(filled_size),
        "notional_usdt": bot.clean_decimal(actual_notional),
        "entry_fee": str(entry_fee if entry_fee is not None else ""),
    })
    try:
        tp = gross_one_percent_tp(
            bot, side, actual_price,
            bot.d(instruments[inst].get("tickSize") or "0.00000001"),
        )
        sl = gross_one_percent_sl(
            bot, side, actual_price,
            bot.d(instruments[inst].get("tickSize") or "0.00000001"),
        )
        tpsl_id, tp_client_id = bot.place_tpsl_for_position(inst, side, tp, sl)
        position["tp"] = bot.clean_decimal(tp)
        position["sl"] = bot.clean_decimal(sl)
        position["tpsl_id"] = tpsl_id
        position["tpsl_client_order_id"] = tp_client_id
        position["protection_status"] = "SMA_PRICE1_TP_SL_ACTIVE"
    except Exception as exc:
        position["protection_status"] = "SMA_TP_SL_SETUP_FAILED"
        position["protection_error"] = str(exc)
        bot.notify(
            f"CRITICAL SMA CROSS {inst}: TP/SL setup failed; closing position: {exc}",
            "BloFin LIVE SAFETY",
        )
        try:
            bot._run_for_tracked_position(
                state, inst, bot.close_tracked_position, "SAFETY: SMA TP/SL setup failed"
            )
        except Exception as close_exc:
            position["protection_error"] += f"; safety close failed: {close_exc}"
            bot.notify(
                f"CRITICAL {inst}: TP/SL setup and safety close both failed: {close_exc}",
                "BloFin LIVE SAFETY",
            )
        return False
    bot.notify(
        f"OPEN {side} {inst} | SMA10/20 cross 10m | entry {actual_price} "
        f"| {position['notional_usdt']} USDT | 1x | broker TP +1% {position['tp']} "
        f"| broker SL -1% {position['sl']} (price, before fees) | exits also on "
        f"opposite RSI14/SMA14 or MA10/MA20 cross at confirmed 10m close",
        "BloFin LIVE SMA CROSS",
    )
    return True


def ensure_broker_sl_for_existing_sma(bot, state, instruments):
    """Upgrade an open SMA TP-only position to combined broker TP+SL protection."""
    for inst, pos in list(bot.get_tracked_positions(state).items()):
        if pos.get("hold_policy") != "SMA10_SMA20_OPPOSITE_CROSS":
            continue
        if pos.get("sl_policy") == "SL_GROSS1_BROKER" and pos.get("sl"):
            continue
        meta = instruments.get(inst)
        if not meta:
            bot.notify(
                f"CRITICAL {inst}: cannot activate requested broker SL -1%; instrument metadata missing",
                "BloFin LIVE SAFETY",
            )
            continue
        try:
            entry = bot.d(pos.get("reference_entry") or "0")
            tick = bot.d(meta.get("tickSize") or "0.00000001")
            if entry <= 0:
                raise ValueError("missing SMA entry price")
            side = pos["side"]
            tp = bot.d(pos.get("tp") or gross_one_percent_tp(bot, side, entry, tick))
            sl = gross_one_percent_sl(bot, side, entry, tick)
            # Never issue an additional position-opening order. First cancel
            # the existing broker TP; replace it with broker-side combined TP/SL.
            if pos.get("tpsl_id"):
                bot.cancel_specific_tpsl(pos)
                pos["tpsl_id"] = ""
                pos["tpsl_client_order_id"] = ""
            try:
                new_id, new_client = bot.place_tpsl_for_position(inst, side, tp, sl)
            except Exception as exc:
                # A position without broker protection must not remain open.
                pos["protection_status"] = "SMA_TP_SL_MIGRATION_FAILED_CLOSE_REQUESTED"
                bot.notify(
                    f"CRITICAL {side} {inst}: new broker TP/SL rejected: {exc}. "
                    "Requesting safety market close.",
                    "BloFin LIVE SAFETY",
                )
                bot._run_for_tracked_position(
                    state, inst, bot.close_tracked_position,
                    "SAFETY: broker TP/SL replacement failed",
                )
                continue
            pos["tp"] = bot.clean_decimal(tp)
            pos["sl"] = bot.clean_decimal(sl)
            pos["sl_policy"] = "SL_GROSS1_BROKER"
            pos["risk_profile"] = "ONE_POSITION_1X_TP_GROSS1_SL_GROSS1_RSI_CROSS_EXIT"
            pos["tpsl_id"] = new_id
            pos["tpsl_client_order_id"] = new_client
            pos["protection_status"] = "SMA_PRICE1_TP_SL_ACTIVE"
            pos.pop("protection_error", None)
            bot.notify(
                f"{side} {inst}: existing position now has broker TP {tp} and SL {sl}",
                "BloFin LIVE TP/SL",
            )
        except Exception as exc:
            # Failed cancellation leaves the old TP intact; do not stack
            # another uncertain conditional order on top of it.
            pos["protection_error"] = str(exc)
            bot.notify(
                f"CRITICAL {inst}: broker SL -1% upgrade not completed: {exc}; "
                "check current exchange protection.",
                "BloFin LIVE SAFETY",
            )


def run(bot, state, top7, tickers, instruments, expected_close_ms, wait_for_confirmed_close):
    if bot.SIGNAL_MINUTES != 10:
        raise RuntimeError("SMA crossover LIVE requires 10m signals")
    if int(state.get("last_scan_close_ms") or 0) == expected_close_ms:
        print("SMA CROSS duplicate 10m scan skipped")
        return
    current_insts = list(bot.get_tracked_positions(state))
    wait_for_confirmed_close(list(dict.fromkeys(top7 + current_insts)), expected_close_ms)
    bot.sync_all_tracked_positions(state)
    ensure_broker_sl_for_existing_sma(bot, state, instruments)
    # Legacy positions keep their existing broker TP/SL and 60m exit.
    bot.evaluate_all_tracked_exit_signals(state, expected_close_ms)
    tracked = bot.get_tracked_positions(state)
    previous_strategy_positions = [
        p for p in tracked.values()
        if p.get("hold_policy") != "SMA10_SMA20_OPPOSITE_CROSS"
    ]
    last = {"signal_close_ms": expected_close_ms, "strategy": "SMA10_SMA20_10m", "events": []}
    if previous_strategy_positions:
        print("SMA CROSS: waiting for pre-existing LIVE position to close under original rules")
        last["status"] = "WAITING_FOR_OLD_POSITION"
    elif tracked:
        # Follow the instrument even after it has left TOP7.
        inst, pos = next(iter(tracked.items()))
        bars = bot.fetch_signal_bars(inst)
        sig = crossover(bot, inst, expected_close_ms, bars=bars)
        try:
            rsi_sig = rsi_average_crossover(
                bot, inst, expected_close_ms, bars=bars,
            )
        except (RuntimeError, ValueError, TypeError) as exc:
            # Missing RSI history must not disable the original SMA exit.
            rsi_sig = {"inst": inst, "cross": None, "error": str(exc)}
        last["events"].append({**sig, "rsi_average": rsi_sig})
        after_entry = expected_close_ms > int(pos.get("signal_close_ms") or 0)
        opposite_ma = after_entry and sig["side"] is not None and sig["side"] != pos["side"]
        opposite_rsi = after_entry and rsi_sig["cross"] is not None and rsi_sig["cross"] != pos["side"]
        if opposite_ma or opposite_rsi:
            # Cancel the broker's combined reduce-only TP/SL before an
            # indicator-based market exit; avoid leftover conditional orders.
            if pos.get("tpsl_id"):
                bot.cancel_specific_tpsl(pos)
                pos["tpsl_id"] = ""
                pos["tpsl_client_order_id"] = ""
                pos["tp"] = ""
            reason = (
                f"RSI14/SMA14 opposite 10m cross: {rsi_sig['cross']}"
                if opposite_rsi else f"SMA10/SMA20 opposite 10m cross: {sig['side']}"
            )
            bot._run_for_tracked_position(
                state, inst, bot.close_tracked_position, reason,
            )
            last["exit_reason"] = reason
            last["status"] = (
                "RSI_OPPOSITE_CROSS_CLOSE_ATTEMPTED" if opposite_rsi
                else "OPPOSITE_CROSS_CLOSE_ATTEMPTED"
            )
            if opposite_ma and not bot.get_tracked_positions(state) and not bot.get_open_positions():
                # Reverse only on an actual opposite price SMA cross; RSI
                # cross is exit-only and never starts a new position by itself.
                last["reversed"] = open_position(
                    bot, state, sig, top7.index(inst) + 1 if inst in top7 else 0, instruments,
                )
        else:
            last["status"] = "HOLDING_UNTIL_TP_OR_OPPOSITE_RSI_OR_MA_CROSS"
    else:
        account_open = bot.get_open_positions()
        if account_open:
            last["status"] = "BLOCKED_UNTRACKED_ACCOUNT_POSITION"
        elif bot.risk_stop_active(state):
            last["status"] = "BLOCKED_EXISTING_DRAWDOWN_STOP"
        else:
            signals = []
            for rank, inst in enumerate(top7, start=1):
                try:
                    sig = crossover(bot, inst, expected_close_ms)
                    if sig["side"]:
                        signals.append((rank, sig))
                except Exception as exc:
                    last["events"].append({"inst": inst, "error": str(exc)})
            last["signals"] = [{"rank": rank, **sig} for rank, sig in signals]
            last["status"] = "NO_CROSS"
            # One coin maximum. Failed preflight can fall back; broker POST errors
            # must stop the scan (never send another uncertain order).
            for rank, sig in signals:
                if state.get("_sma_order_post_started"):
                    last["status"] = "ONE_ORDER_ALREADY_ATTEMPTED"
                    break
                try:
                    if open_position(bot, state, sig, rank, instruments):
                        last["status"] = "OPENED"
                        last["selected"] = sig
                        break
                except Exception as exc:
                    # A stale quote, excessive spread, or other pre-order
                    # validation failure must not discard later TOP7 crosses.
                    # Once POST /trade/order starts, outcome may be uncertain:
                    # never try a different coin in the same scan.
                    post_started = bool(state.get("_sma_order_post_started"))
                    last["events"].append({
                        "inst": sig["inst"],
                        "side": sig["side"],
                        "reason": str(exc),
                        "status": (
                            "ORDER_ATTEMPT_ERROR_STOP" if post_started
                            else "PRE_ORDER_REJECTED_TRY_NEXT"
                        ),
                    })
                    if post_started:
                        last["status"] = "ORDER_ATTEMPT_ERROR_STOP"
                        last["error"] = str(exc)
                        bot.notify(
                            f"SMA CROSS {sig['inst']}: order attempt uncertain: {exc}",
                            "BloFin LIVE ERROR",
                        )
                        break
                    last["status"] = "PRE_ORDER_REJECTED_TRY_NEXT"
                    print(f"SMA CROSS {sig['inst']} pre-order rejected; try next TOP7: {exc}")
                    continue
    state.pop("_sma_order_post_started", None)
    state["ma_cross_last_scan"] = last
    state.setdefault("last_diagnostic", {})["ma_cross"] = last
    state["last_diagnostic"]["result"] = last["status"]
    state["last_diagnostic"]["strategy"] = "SMA10_SMA20_10m"
    state["last_scan_close_ms"] = expected_close_ms
    state["last_run_ms"] = bot.now_ms()
    state["last_top7"] = top7
    bot.save_state(state)
    print(f"SMA CROSS result: {last['status']}")
