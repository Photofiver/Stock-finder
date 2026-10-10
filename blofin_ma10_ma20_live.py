"""LIVE SMA10/SMA20 price crossover strategy for 10m BloFin TOP7.

New SMA positions have no TP/SL or time exit: close on opposite SMA cross.
Legacy positions retain their original broker protection until naturally closed.
Only one account position is permitted while this strategy is active.
"""
import os
import uuid


def crossover(bot, inst, expected_close_ms):
    bars = bot.fetch_signal_bars(inst)
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
    quote = bot.checked_preorder_quote(inst, side, signal["close"])
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
        "tp_policy": "NONE",
        "sl_policy": "NONE",
        "hold_policy": "SMA10_SMA20_OPPOSITE_CROSS",
        "protection_status": "SMA_CROSS_ONLY_NO_BROKER_TPSL",
        "strategy": f"SMA10_SMA20_{side}_10m",
        "code_commit": bot.CODE_COMMIT,
        "risk_profile": "ONE_POSITION_1X_NO_TPSL",
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
    position.update({
        "reference_entry": bot.clean_decimal(actual_price),
        "filled_size": bot.clean_decimal(filled_size),
        "notional_usdt": bot.clean_decimal(filled_size * contract_value * actual_price),
        "entry_fee": str(fill.get("fee") or "0"),
    })
    bot.notify(
        f"OPEN {side} {inst} | SMA10/20 cross 10m | entry {actual_price} "
        f"| {position['notional_usdt']} USDT | 1x | hold until opposite cross "
        "| no broker TP/SL (price risk)",
        "BloFin LIVE SMA CROSS",
    )
    return True


def run(bot, state, top7, tickers, instruments, expected_close_ms, wait_for_confirmed_close):
    if bot.SIGNAL_MINUTES != 10:
        raise RuntimeError("SMA crossover LIVE requires 10m signals")
    if int(state.get("last_scan_close_ms") or 0) == expected_close_ms:
        print("SMA CROSS duplicate 10m scan skipped")
        return
    current_insts = list(bot.get_tracked_positions(state))
    wait_for_confirmed_close(list(dict.fromkeys(top7 + current_insts)), expected_close_ms)
    bot.sync_all_tracked_positions(state)
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
        sig = crossover(bot, inst, expected_close_ms)
        last["events"].append(sig)
        if sig["side"] and sig["side"] != pos["side"] and expected_close_ms > int(pos.get("signal_close_ms") or 0):
            bot._run_for_tracked_position(
                state, inst, bot.close_tracked_position,
                f"SMA10/SMA20 opposite 10m cross: {sig['side']}",
            )
            last["status"] = "OPPOSITE_CROSS_CLOSE_ATTEMPTED"
            if not bot.get_tracked_positions(state) and not bot.get_open_positions():
                # Reverse only after exchange confirms the old position is flat.
                last["reversed"] = open_position(bot, state, sig, top7.index(inst)+1 if inst in top7 else 0, instruments)
        else:
            last["status"] = "HOLDING_UNTIL_OPPOSITE_CROSS"
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
                try:
                    if open_position(bot, state, sig, rank, instruments):
                        last["status"] = "OPENED"
                        last["selected"] = sig
                        break
                except Exception as exc:
                    last["status"] = "ORDER_ATTEMPT_ERROR_STOP"
                    last["error"] = str(exc)
                    bot.notify(f"SMA CROSS {sig['inst']} order attempt failed: {exc}", "BloFin LIVE ERROR")
                    break
    state["ma_cross_last_scan"] = last
    state["last_scan_close_ms"] = expected_close_ms
    state["last_run_ms"] = bot.now_ms()
    state["last_top7"] = top7
    bot.save_state(state)
    print(f"SMA CROSS result: {last['status']}")
