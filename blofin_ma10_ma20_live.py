"""LIVE SMA10/SMA20 entry strategy for 10m BloFin TOP7.

New entries require Wilder ADX(14) < 38 on the closed signal candle.
New positions use broker TP +1% and broker SL at the last confirmed SMA10.
The broker triggers the MA10 stop intrabar; its threshold is refreshed every 10m.
Pre-existing SMA positions retain their original 1% SL / RSI / MA-cross exits.
Legacy non-SMA positions retain their original protection until closed.
Only one account position is permitted.
"""
import uuid


PRICE_TAKE_PROFIT_PCT = 0.01
PRICE_STOP_LOSS_PCT = 0.01
RSI_AVERAGE_PERIOD = 14
ADX_MAX_EXCLUSIVE = 38.0
DYNAMIC_MA10_HOLD_POLICY = "SMA10_DYNAMIC_BROKER_STOP"


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


def ma10_stop_price(bot, side, ma10, tick):
    """Convert last closed SMA10 to a protective BloFin broker SL price."""
    value = bot.d(ma10)
    if value <= 0:
        raise ValueError("Missing SMA10 for stop")
    if side == "LONG":
        return bot.price_step(value, tick, bot.ROUND_CEILING)
    if side == "SHORT":
        return bot.price_step(value, tick, bot.ROUND_FLOOR)
    raise ValueError("Unknown side for MA10 stop")


def adx_allows_entry(adx):
    return adx is not None and 0 <= float(adx) < ADX_MAX_EXCLUSIVE


def crossover(bot, inst, expected_close_ms, bars=None):
    bars = bot.fetch_signal_bars(inst) if bars is None else bars
    if len(bars) < 21 or bot.bar_close_ms(bars[-1]) != expected_close_ms:
        raise RuntimeError(f"{inst}: expected confirmed 10m close unavailable")
    closes = [float(bar["c"]) for bar in bars]
    old_fast = sum(closes[-11:-1]) / 10
    old_slow = sum(closes[-21:-1]) / 20
    new_fast = sum(closes[-10:]) / 10
    new_slow = sum(closes[-20:]) / 20
    # Reuse the exact Wilder ADX(14) calculation from the historical audit.
    from blofin_live_scan_audit import adx14_and_obv5
    adx14, _ = adx14_and_obv5(bars)
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
        "adx14": adx14,
        "adx_entry_allowed": adx_allows_entry(adx14),
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
    # This check also protects direct opens and reversals, not just TOP7 discovery.
    if not adx_allows_entry(signal.get("adx14")):
        print(f"SMA CROSS {inst} {side}: ADX14={signal.get('adx14')} blocked (must be < {ADX_MAX_EXCLUSIVE})")
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
    tick = bot.d(instruments[inst].get("tickSize") or "0.00000001")
    ma_sl = ma10_stop_price(bot, side, signal["ma10"], tick)
    # Never open once the live quote has already crossed the proposed MA10 stop.
    if (side == "LONG" and bot.d(quote["bid"]) <= ma_sl) or (
        side == "SHORT" and bot.d(quote["ask"]) >= ma_sl
    ):
        print(f"SMA CROSS {inst} {side}: entry blocked; price already beyond MA10 stop {ma_sl}")
        return False
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
        "sl_policy": "SL_MA10_BROKER_REFRESH_10M",
        "hold_policy": DYNAMIC_MA10_HOLD_POLICY,
        "protection_status": "SMA_MA10_TP_SL_PENDING",
        "strategy": f"SMA10_SMA20_{side}_10m",
        "code_commit": bot.CODE_COMMIT,
        "risk_profile": "ONE_POSITION_1X_TP_GROSS1_SL_MA10_ADX_LT38",
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
        sl = ma10_stop_price(bot, side, signal["ma10"], tick)
        if (side == "LONG" and sl >= actual_price) or (
            side == "SHORT" and sl <= actual_price
        ):
            raise RuntimeError(f"MA10 stop {sl} already crossed at fill {actual_price}")
        tpsl_id, tp_client_id = bot.place_tpsl_for_position(inst, side, tp, sl)
        if not tpsl_id:
            raise RuntimeError("Broker did not return TP/SL identifier")
        position["tp"] = bot.clean_decimal(tp)
        position["sl"] = bot.clean_decimal(sl)
        position["tpsl_id"] = tpsl_id
        position["tpsl_client_order_id"] = tp_client_id
        position["protection_status"] = "SMA_MA10_TP_SL_ACTIVE"
        position["ma10_stop_signal_close_ms"] = signal["signal_close_ms"]
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
        f"| broker MA10 stop {position['sl']} (updated every 10m; triggers intrabar) "
        f"| ADX14 {signal['adx14']:.2f} < 38",
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



def refresh_dynamic_ma10_stop(bot, state, pos, bars, expected_close_ms, instruments):
    """Refresh live exchange SL to last CLOSED SMA10; exchange triggers intrabar."""
    inst, side = pos["inst"], pos["side"]
    if expected_close_ms <= int(pos.get("signal_close_ms") or 0):
        return "HOLDING_INITIAL_MA10_STOP"
    sig = crossover(bot, inst, expected_close_ms, bars=bars)
    meta = instruments.get(inst)
    if not meta:
        # Never cancel the existing broker SL if a safe replacement is unavailable.
        return "HOLDING_OLD_MA10_STOP_NO_INSTRUMENT_METADATA"
    tick = bot.d(meta.get("tickSize") or "0.00000001")
    target = ma10_stop_price(bot, side, sig["ma10"], tick)
    old_sl = bot.d(pos.get("sl") or "0")
    if old_sl == target:
        return "HOLDING_MA10_STOP_CURRENT"
    rows = bot.market_get("/api/v1/market/tickers", {"instId": inst})
    quote = next((r for r in rows if isinstance(r, dict) and r.get("instId") == inst), None)
    if not quote:
        return "HOLDING_OLD_MA10_STOP_NO_QUOTE"
    last = bot.d(quote.get("last") or "0")
    quote_ms = int(quote.get("ts") or 0)
    age_ms = bot.now_ms() - quote_ms
    if last <= 0 or quote_ms <= 0 or not (-5000 <= age_ms <= bot.MAX_ENTRY_QUOTE_AGE_MS):
        return "HOLDING_OLD_MA10_STOP_STALE_QUOTE"
    tp = bot.d(pos.get("tp") or "0")
    if tp <= 0:
        return "HOLDING_OLD_MA10_STOP_NO_TP"
    stop_crossed = (side == "LONG" and last <= target) or (
        side == "SHORT" and last >= target
    )
    stop_beyond_tp = (side == "LONG" and target >= tp) or (
        side == "SHORT" and target <= tp
    )
    if stop_crossed or stop_beyond_tp:
        # A moving stop is already through market/TP: close rather than issue
        # an invalid conditional order that might remain dormant.
        if pos.get("tpsl_id"):
            try:
                bot.cancel_specific_tpsl(pos)
            except Exception as exc:
                print(f"MA10 STOP {inst}: TP/SL cancel failed ({exc}); retaining existing order")
                return "MA10_EXIT_CANCEL_FAILED_OLD_PROTECTION_RETAINED"
            pos["tpsl_id"] = ""
            pos["tpsl_client_order_id"] = ""
        bot._run_for_tracked_position(
            state, inst, bot.close_tracked_position,
            f"MA10 stop reached at latest price {last}; confirmed SMA10 {target}",
        )
        return "MA10_STOP_MARKET_EXIT"
    if not pos.get("tpsl_id"):
        # Unknown broker protection state: do not open a duplicate position.
        bot._run_for_tracked_position(
            state, inst, bot.close_tracked_position, "SAFETY: MA10 TP/SL identifier missing"
        )
        return "MA10_MISSING_BROKER_ORDER_CLOSE_ATTEMPTED"
    try:
        bot.cancel_specific_tpsl(pos)
    except Exception as exc:
        # An unsuccessful cancel must not be followed by a second reduce-only order.
        print(f"MA10 STOP {inst}: retaining prior broker TP/SL after cancel failure: {exc}")
        return "MA10_STOP_UPDATE_CANCEL_FAILED"
    pos["tpsl_id"] = ""
    pos["tpsl_client_order_id"] = ""
    try:
        new_id, new_client = bot.place_tpsl_for_position(inst, side, tp, target)
        if not new_id:
            raise RuntimeError("Broker did not return new TP/SL identifier")
    except Exception as exc:
        # Cancelling succeeded: fail closed rather than leave a LIVE position unprotected.
        pos["protection_status"] = "MA10_STOP_UPDATE_FAILED_CLOSE_REQUESTED"
        pos["protection_error"] = str(exc)
        bot.notify(f"CRITICAL {side} {inst}: MA10 SL replacement failed: {exc}; closing position",
                   "BloFin LIVE SAFETY")
        bot._run_for_tracked_position(
            state, inst, bot.close_tracked_position, "SAFETY: MA10 stop replacement failed"
        )
        return "MA10_STOP_REPLACEMENT_FAILED_CLOSE_ATTEMPTED"
    pos["tpsl_id"] = new_id
    pos["tpsl_client_order_id"] = new_client
    pos["sl"] = bot.clean_decimal(target)
    pos["ma10_stop_signal_close_ms"] = expected_close_ms
    pos["protection_status"] = "SMA_MA10_TP_SL_ACTIVE"
    print(f"MA10 STOP {side} {inst}: updated {old_sl} -> {target} (TP {tp})")
    return "MA10_BROKER_STOP_UPDATED"


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
        if p.get("hold_policy") not in (
            "SMA10_SMA20_OPPOSITE_CROSS", DYNAMIC_MA10_HOLD_POLICY,
        )
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
        if pos.get("hold_policy") == DYNAMIC_MA10_HOLD_POLICY:
            # New policy: only broker TP +1% or MA10 stop; no RSI/MA20 exits.
            last["events"].append(sig)
            last["status"] = refresh_dynamic_ma10_stop(
                bot, state, pos, bars, expected_close_ms, instruments
            )
        else:
            # Existing positions keep their old RSI and MA10/MA20 exit policy.
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
                        if not adx_allows_entry(sig.get("adx14")):
                            last["events"].append({
                                "inst": inst, "side": sig["side"],
                                "status": "ADX_GE_38_BLOCKED",
                                "adx14": sig.get("adx14"),
                            })
                            continue
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
