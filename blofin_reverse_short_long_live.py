"""BloFin LIVE: reverse the fixed 2026-10-18 20W/0L SHORT setup into LONG.

Entry: 5m RED directly after GREEN; 3x <= RED volume < 4x previous
GREEN volume, and RED volume <= mean of ten preceding closed 5m bars.
Coin closed-bar 150m return >=0; BTC last 15m change <= +0.062055813%;
last COMPLETED coin 15m return >= -0.10322961%; signal RED NOT an
inside bar. Enter LONG after close, next 5m opening cycle.

Exit: in a position, update last_red_volume on every qualifying RED
(volume <= previous 10 mean). On qualifying GREEN with volume STRICTLY
larger than last qualifying RED, CLOSE at next 5m opening cycle.
No TP, no SL, no +DI, no time-based exit for THIS strategy.
One position per coin, multiple different coins if budget permits.

Existing positions preserve their original protection and exit policy.
This module never clears the broker's existing TPSL. Safety interlocks
block uncertain orders; 1x isolated and bankroll limits remain in force.
"""
import math
import os
import uuid
from decimal import Decimal

STRATEGY = "REV_SHORT_20W_LONG_VOLUME_V1"
HOLD_POLICY = "VOLUME_REV_SHORT_LONG"
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


def _completed_coin15_pct(series, ts):
    """Must be a FULLY COMPLETED 15m block, never the running 15m candle."""
    block = 3 * FIVE
    last_block_start = ((ts + FIVE)//block - 1)*block
    end = series.get(last_block_start + 2*FIVE)
    before = series.get(last_block_start - FIVE)
    if end is None or before is None:
        return None
    return 100.0 * (end[3]/before[3] - 1.0)


def entry_signal(bot, bars, btc_bars, expected_close_ms):
    if bot.SIGNAL_MINUTES != 5:
        return None
    ts = int(expected_close_ms) - FIVE
    own = _by_ts(bars, int(expected_close_ms))
    btc = _by_ts(btc_bars, int(expected_close_ms))
    b = own.get(ts)
    prev = own.get(ts - FIVE)
    past150 = own.get(ts - 30*FIVE)
    if b is None or prev is None or past150 is None:
        return None
    if not (prev[3] > prev[0] and b[3] < b[0] and prev[4] > 0):
        return None
    vol_ratio = b[4]/prev[4]
    if not (VOL_MIN <= vol_ratio < VOL_MAX_EXCLUSIVE):
        return None
    mean10 = _previous10_mean(own, ts)
    if mean10 is None or mean10 <= 0 or b[4] > mean10:
        return None
    prior150 = 100 * (b[3]/past150[3] - 1)
    if prior150 < 0:
        return None
    last15 = _completed_coin15_pct(own, ts)
    if last15 is None or last15 < COIN15_MIN:
        return None
    # Exactly "not inside": candle escapes previous candle's high/low range.
    if b[1] <= prev[1] and b[2] >= prev[2]:
        return None
    btc_cur, btc_prev = btc.get(ts), btc.get(ts - 3*FIVE)
    if btc_cur is None or btc_prev is None:
        return None
    btc15 = 100 * (btc_cur[3]/btc_prev[3] - 1)
    if btc15 > BTC15_MAX:
        return None
    return {
        "side":"LONG", "strategy":STRATEGY,
        "signal_close_ms":int(expected_close_ms),
        "close":b[3], "signal_red_volume":b[4],
        "previous_green_volume":prev[4], "volume_ratio":vol_ratio,
        "mean10_prev":mean10,
        "coin_prior150_pct":prior150,
        "coin_completed15_pct":last15, "btc_prior15_pct":btc15,
        "not_inside_bar":True, "signal_candle_ts":ts,
    }


def exit_transition(pos, bars, expected_close_ms):
    """Return (signal_to_close, new_last_red_volume).
    Process one CLOSED 5m candle only. Never act on stale or future bars.
    """
    ts = int(expected_close_ms) - FIVE
    rows = _by_ts(bars, int(expected_close_ms))
    b = rows.get(ts)
    mean10 = _previous10_mean(rows, ts)
    old_red = float(pos.get("last_red_volume") or pos.get("signal_red_volume") or 0)
    if b is None or mean10 is None or mean10 <= 0 or b[4] > mean10:
        return False, old_red
    if b[3] < b[0]:
        return False, b[4]
    if b[3] > b[0] and b[4] > old_red:
        return True, old_red
    return False, old_red


def _open_one(bot, state, sig, instruments):
    inst = sig["inst"]
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
    quote = bot.checked_preorder_quote(inst, "LONG", sig["close"],
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
            "positionSide":"net","side":"buy","orderType":"market",
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
        "inst":inst,"side":"LONG","opened_ms":now,
        "signal_close_ms":int(sig["signal_close_ms"]),
        "signal_age_ms":now-int(sig["signal_close_ms"]),
        "signal_rank":int(sig["rank"]),
        "signal_snapshot":dict(sig,pre_order_quote=quote),
        "strategy":STRATEGY,"hold_policy":HOLD_POLICY,
        "last_red_volume":sig["signal_red_volume"],
        "last_checked_signal_close_ms":int(sig["signal_close_ms"]),
        "order_id":str(row.get("orderId") or ""),
        "client_order_id":order_id,
        "size":bot.clean_decimal(size),
        "reference_entry":"",
        "filled_size":"",
        "notional_usdt":bot.clean_decimal(expected_notional),
        "tp":"","sl":"","tp_policy":"NONE","sl_policy":"NONE",
        "risk_profile":"1X_VOLUME_FLIP_NO_TPSL",
        "protection_status":"NO_TPSL_USER_REQUESTED",
        "code_commit":bot.CODE_COMMIT,
    }
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
    attempt["status"]="TRACKED"
    bot.save_state(state)
    try:
        from blofin_trade_charts import capture_chart
        pos["entry_chart"]=capture_chart(bot,pos,"ENTRY",now,actual_price)
    except Exception as exc:
        pos["entry_chart_error"]=f"{type(exc).__name__}: {exc}"
    bot.notify(
        f"OPEN LONG {inst} | rank {sig['rank']}/200 | vol {sig['volume_ratio']:.2f}x "
        f"| BTC15 {sig['btc_prior15_pct']:+.3f}% | coin15 {sig['coin_completed15_pct']:+.3f}% "
        f"| 1x isolated, notional {pos['notional_usdt']} USDT | NO TP/SL "
        "| exit: next qualifying GREEN vol > last qualifying RED vol.",
        "BloFin LIVE volume reversal LONG",
        attachment_path=pos.get("entry_chart"),
    )
    return True


def run(bot, state, top200, tickers, instruments, expected_close_ms, wait_for_confirmed_close):
    if bot.SIGNAL_MINUTES!=5 or bot.TOP_N!=200 or len(top200)!=200:
        raise RuntimeError("Volume reversal LONG must use confirmed 5m and TOP200")
    if int(state.get("last_scan_close_ms") or 0)==expected_close_ms:
        print(f"{STRATEGY}: duplicate scan skipped")
        return
    scan={"strategy":STRATEGY,"signal_close_ms":int(expected_close_ms),
          "top200_count":len(top200),"status":"PENDING",
          "scanned_confirmed":0,"matched":0,"candidates":[],
          "entries":[],"exits":[],"rejections":[]}

    bot.sync_all_tracked_positions(state)
    bot.evaluate_all_tracked_exit_signals(state, expected_close_ms)
    active = bot.get_tracked_positions(state)
    broker = bot.get_open_positions()
    broker_set={str(p.get("instId")) for p in broker}
    # Preserve old positions' TP/SL. Existing old positions stay managed by
    # their prior policy; do not change, cancel or reprice their orders.
    untracked=broker_set-set(active)
    if untracked:
        scan["status"]="BLOCK_UNTRACKED_ACCOUNT_POSITIONS"
        scan["untracked"]=sorted(untracked)
    pending=state.get("volume_long_order_attempts") or {}
    uncertain=[i for i,a in pending.items()
               if a.get("status") in ("POST_STARTED","UNCERTAIN","BROKER_ACCEPTED","UNCERTAIN_FILL")]
    old_attempt=state.get("red_sma_order_attempt") or {}
    if old_attempt.get("status") in ("POST_STARTED","UNCERTAIN","ACCEPTED_BROKER"):
        uncertain.append("OLD_RED_ORDER")
    if uncertain:
        scan["status"]="BLOCK_UNCERTAIN_ORDERS"
        scan["uncertain_instruments"]=uncertain

    # Every open coin must be confirmed for exits even when it fell outside TOP200.
    requested=list(dict.fromkeys(list(top200)+["BTC-USDT"]+list(active)))
    confirmed = wait_for_confirmed_close(requested, expected_close_ms) or {}
    scan["scanned_confirmed"]=sum(1 for inst in top200 if inst in confirmed)
    for inst,pos in list(active.items()):
        if pos.get("strategy")!=STRATEGY or pos.get("hold_policy")!=HOLD_POLICY:
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
        should_close,new_red=exit_transition(pos,bars,expected_close_ms)
        pos["last_red_volume"]=new_red
        pos["last_checked_signal_close_ms"]=int(expected_close_ms)
        if should_close:
            # Broker close acknowledged before recording completion.
            try:
                bot._run_for_tracked_position(
                    state,inst,bot.close_tracked_position,
                    "VOLUME_REVERSAL: GREEN volume > last qualifying RED")
                if inst in bot.get_tracked_positions(state):
                    bot.get_tracked_positions(state)[inst]["exit_pending"]=True
                scan["exits"].append(inst)
            except Exception as exc:
                pos["exit_pending"]="UNCERTAIN_CLOSE"
                scan["rejections"].append({"inst":inst,
                    "reason":"UNCERTAIN_BROKER_CLOSE",
                    "error":f"{type(exc).__name__}: {exc}"})
                bot.notify(f"CRITICAL {inst}: exit uncertain: {exc}",
                           "BloFin LIVE volume exit safety")
    # Do not trade until any uncertain broker order or closure is reconciled.
    if os.getenv("LIVE_VOLUME_REVERSE_LONG_PAUSE_ENTRIES", "true").strip().lower() == "true":
        scan["status"]="PAUSED_NEW_ENTRIES_KEEP_EXISTING_EXITS"
    elif scan["status"] in ("BLOCK_UNTRACKED_ACCOUNT_POSITIONS","BLOCK_UNCERTAIN_ORDERS"):
        pass
    elif any(p.get("exit_pending") for p in bot.get_tracked_positions(state).values()):
        scan["status"]="WAIT_FOR_EXIT_RECONCILIATION"
    elif not (0<=bot.now_ms()-expected_close_ms<=min(bot.SIGNAL_MAX_AGE_MS,AGE_MAX_MS)):
        scan["status"]="STALE_5M_SCAN"
    elif "BTC-USDT" not in confirmed or int(bot.bar_close_ms(confirmed["BTC-USDT"][-1]))!=expected_close_ms:
        scan["status"]="BTC_5M_NOT_CONFIRMED"
    elif bot.risk_stop_active(state):
        scan["status"]="BLOCKED_10PCT_DRAWDOWN"
    else:
        btc=confirmed["BTC-USDT"]
        matches=[]
        for rank,inst in enumerate(top200,1):
            if inst in bot.get_tracked_positions(state):continue
            if inst in broker_set:continue
            bars=confirmed.get(inst)
            if not bars or int(bot.bar_close_ms(bars[-1]))!=expected_close_ms:continue
            sig=entry_signal(bot,bars,btc,expected_close_ms)
            if sig is not None:
                sig.update({"inst":inst,"rank":rank})
                matches.append(sig)
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
