"""10m LIVE TOP7 selection using strictly all >110-frequency winner filters.

At most one new order per scan; preserve order safety, exposure and fallback.
"""


def initialize_bankroll(state):
    import blofin_live_hourly as bot

    if state.get("bankroll_mode") == "NEAREST_VOLUME_V1":
        return False
    if bot.get_tracked_positions(state):
        raise RuntimeError("Cannot reset bankroll while tracked LIVE positions remain open")
    bankroll = bot.MAX_NOTIONAL_USDT
    if bankroll <= 0:
        raise RuntimeError("Invalid starting LIVE bankroll")
    state.setdefault("bankroll_rebase_history", []).append({
        "at_ms": bot.now_ms(),
        "previous_bankroll_start_usdt": state.get("bankroll_start_usdt"),
        "previous_pnl_baseline_usdt": state.get("bankroll_pnl_baseline_usdt"),
        "new_bankroll_start_usdt": bot.clean_decimal(bankroll),
        "new_pnl_baseline_usdt": bot.clean_decimal(
            bot.d(state.get("realized_pnl_usdt") or 0)
        ),
    })
    state["bankroll_start_usdt"] = bot.clean_decimal(bankroll)
    state["bankroll_pnl_baseline_usdt"] = bot.clean_decimal(
        bot.d(state.get("realized_pnl_usdt") or 0)
    )
    state["bankroll_mode"] = "NEAREST_VOLUME_V1"
    state["bankroll_rebased_at_ms"] = bot.now_ms()
    return True


def score_options(bot, bars, i):
    """Strict >110 occurrences strategy; AND all rules for each direction."""
    if i < 40 or i >= len(bars):
        return []
    current, previous = bars[i], bars[i - 1]
    current_hist = current.get("macd_hist")
    previous_hist = previous.get("macd_hist")
    rsi = current.get("rsi")
    if current_hist is None or previous_hist is None or rsi is None:
        return []

    # OBV close minus OBV five closed bars earlier.
    obv_change = sum(
        float(bars[j]["v"]) * (
            1 if bars[j]["c"] > bars[j - 1]["c"]
            else -1 if bars[j]["c"] < bars[j - 1]["c"] else 0
        )
        for j in range(i - 4, i + 1)
    )
    volume = float(current["v"])
    volume_ma5 = bot.volume_ma(bars, i, 5)
    long_metrics = bot.long_entry_metrics(bars, i)
    if long_metrics is None or volume_ma5 is None:
        return []

    checks = {
        "LONG": [
            ("RSI_LT_67", float(rsi) < 67.0),
            ("OBV_RISING_5", obv_change > 0),
            ("MACD_HIST_NEGATIVE", float(current_hist) < 0),
            ("MACD_HIST_RISING", float(current_hist) > float(previous_hist)),
            ("VOLUME_BELOW_MA5", volume < float(volume_ma5)),
            ("VOLUME_GT_LAST_RED", bool(long_metrics["volume_higher_than_last_red"])),
        ],
        "SHORT": [
            ("OBV_RISING_5", obv_change > 0),
            ("MACD_HIST_NEGATIVE", float(current_hist) < 0),
            ("RSI_GE_55", float(rsi) >= 55.0),
        ],
    }
    last_red = long_metrics.get("last_red_volume")
    advantage = (
        ((volume / float(last_red)) - 1.0) * 100.0
        if last_red not in (None, 0) else None
    )
    options = []
    for side in ("LONG", "SHORT"):
        tests = checks[side]
        missing = [name for name, passed in tests if not passed]
        passed_count = len(tests) - len(missing)
        options.append({
            "side": side,
            "score": f"{passed_count}/{len(tests)}",
            "score_ratio": passed_count / len(tests),
            "missing": missing,
            "missing_count": len(missing),
            "entry_eligible": not missing,
            "volume_rule_passed": bool(long_metrics["volume_higher_than_last_red"]) if side == "LONG" else None,
            "volume_advantage_pct": advantage if side == "LONG" else None,
            "obv_delta_5": obv_change,
            "volume_ma5": volume_ma5,
            "rsi14": float(rsi),
            "macd_hist": float(current_hist),
            "macd_hist_previous": float(previous_hist),
        })
    return options


def volume_snapshot_passes(snapshot, side):
    """Revalidate ALL >110 entry rules using the exact closed candle.

    The name is retained for compatibility with the existing broker safety gate.
    Fail closed on absent or changed signal data.
    """
    if side not in ("LONG", "SHORT") or snapshot.get("snapshot_error"):
        return False
    signal_close_ms = int(snapshot.get("signal_close_ms") or 0)
    inst = str(snapshot.get("inst") or "")
    if not inst or not signal_close_ms:
        return False
    import blofin_live_hourly as bot
    bars = bot.fetch_signal_bars(inst)
    signal_idx = next(
        (i for i, bar in enumerate(bars)
         if bot.bar_close_ms(bar) == signal_close_ms),
        None,
    )
    if signal_idx is None:
        return False
    if float(bars[signal_idx]["c"]) != float(snapshot.get("close") or 0):
        return False
    return any(
        option["side"] == side and option["entry_eligible"]
        for option in score_options(bot, bars, signal_idx)
    )


def select_signals(state, top7):
    import blofin_live_hourly as bot

    observed_ms = bot.now_ms()
    processed = state.setdefault("last_processed_close_ms", {})
    options, errors = [], []
    for rank, inst in enumerate(top7, start=1):
        try:
            bars = bot.fetch_signal_bars(inst)
            if len(bars) < 40:
                raise ValueError("Insufficient confirmed signal bars")
            i = len(bars) - 1
            signal_close_ms = bot.bar_close_ms(bars[i])
            if signal_close_ms <= int(processed.get(inst) or 0):
                continue
            for option in score_options(bot, bars, i):
                option.update({
                    "inst": inst, "rank_in_top7": rank,
                    "signal_close_ms": signal_close_ms,
                    "signal_age_ms": observed_ms - signal_close_ms,
                })
                options.append(option)
            processed[inst] = max(int(processed.get(inst) or 0), signal_close_ms)
        except Exception as exc:
            errors.append({"inst": inst, "error": f"{type(exc).__name__}: {exc}"})
            print(f"GT110 data unavailable {inst}: {exc}")

    # Rank TOP7 first. If both directions qualify on one coin, prefer LONG.
    ordered = sorted(
        [row for row in options
         if row["entry_eligible"]
         and 0 <= row["signal_age_ms"] <= bot.SIGNAL_MAX_AGE_MS],
        key=lambda row: (row["rank_in_top7"], row["inst"],
                         0 if row["side"] == "LONG" else 1),
    )
    state["last_pretrade_ranking"] = {
        "generated_at_ms": bot.now_ms(),
        "strategy": "HISTORICAL_GT110_STRICT",
        "conditions_long": 6,
        "conditions_short": 3,
        "max_new_positions_per_scan": 1,
        "fallback_after_preorder_rejection": True,
        "not_a_profit_prediction": True,
        "scores": sorted(
            options,
            key=lambda row: (row["missing_count"],
                             row["rank_in_top7"],
                             0 if row["side"] == "LONG" else 1),
        ),
        "errors": errors,
        "eligible_entry_order": [{
            "inst": row["inst"], "side": row["side"],
            "top7_rank": row["rank_in_top7"],
            "score": row["score"],
            "missing": row["missing"],
            "priority": idx,
        } for idx, row in enumerate(ordered, start=1)],
        "entry_attempts": [],
    }
    if not ordered:
        print("GT110 no entry: no fresh fully qualified TOP7 candidate")
        return []
    print("GT110 strict entry order: " + ", ".join(
        f"{row['inst']} {row['side']} {row['score']}" for row in ordered
    ))
    return [
        (row["rank_in_top7"], row["inst"], row["side"], row["signal_close_ms"])
        for row in ordered
    ]


def execute_with_fallback(state, candidates, tickers, instruments):
    """Try volume-eligible choices in ranked order, at most one new LIVE order.

    Every rejection before the market-order POST may fall back. Once POST starts,
    never try another coin, even if the broker status is uncertain; duplicate
    orders are more dangerous than missing a 15m entry.
    """
    import blofin_live_hourly as bot

    ranking = state.setdefault("last_pretrade_ranking", {})
    attempts = ranking.setdefault("entry_attempts", [])
    ranking["executed_candidate"] = None

    def log_attempt(candidate, status, message="", priority=None):
        rank, inst, side, signal_close_ms = candidate
        record = {
            "inst": inst, "side": side, "top7_rank": rank,
            "signal_close_ms": signal_close_ms,
            "priority": priority,
            "status": status,
            "reason": message,
        }
        attempts.append(record)
        print(
            f"NEAREST_VOLUME FALLBACK {inst} {side} "
            f"priority={priority} status={status} reason={message}"
        )
        return record

    if not candidates:
        return []
    if bot.risk_stop_active(state):
        log_attempt(candidates[0], "BLOCKED_RISK_STOP", "10% max drawdown")
        return []

    positions = bot.get_tracked_positions(state)
    open_rows = bot.get_open_positions()
    account_open = {
        str(row.get("instId")) for row in open_rows if row.get("instId")
    }
    untracked = sorted(account_open - set(positions))
    if untracked:
        log_attempt(
            candidates[0], "BLOCKED_UNTRACKED_POSITIONS",
            ", ".join(untracked),
        )
        return []
    if len(account_open) >= bot.MAX_OPEN_POSITIONS:
        log_attempt(
            candidates[0], "BLOCKED_NO_SLOT",
            f"All {bot.MAX_OPEN_POSITIONS} LIVE slots occupied",
        )
        return []

    try:
        exposure = sum(
            (abs(bot.d(pos.get("notional_usdt") or "0"))
             for pos in positions.values()), bot.Decimal("0")
        )
        if any(bot.d(pos.get("notional_usdt") or "0") <= 0
               for pos in positions.values()):
            raise ValueError("Tracked position notional missing or invalid")
    except Exception as exc:
        log_attempt(candidates[0], "BLOCKED_TRACKING_ERROR", str(exc))
        return []

    live_bankroll = bot.current_live_bankroll(state)
    remaining_bankroll = live_bankroll - exposure
    if remaining_bankroll <= 0:
        log_attempt(
            candidates[0], "BLOCKED_BANKROLL",
            f"No free LIVE bankroll: {live_bankroll} USDT",
        )
        return []
    available = bot.get_available_usdt()
    budget = min(available, remaining_bankroll)
    if budget <= 0:
        log_attempt(candidates[0], "BLOCKED_NO_FUNDS", "No available USDT")
        return []

    for priority, candidate in enumerate(candidates, start=1):
        rank, inst, side, signal_close_ms = candidate
        if inst in account_open:
            log_attempt(
                candidate, "SKIPPED_ALREADY_OPEN",
                "An open position already exists for this instrument",
                priority,
            )
            continue
        age_ms = bot.now_ms() - int(signal_close_ms)
        if age_ms < 0 or age_ms > bot.SIGNAL_MAX_AGE_MS:
            log_attempt(
                candidate, "BLOCKED_STALE_SIGNAL",
                f"Signal age {age_ms}ms exceeds allowed age",
                priority,
            )
            break

        # Preflight is entirely read-only and cannot open an exchange position.
        # Keep the same directional volume and executable-price guards as
        # place_live_trade(), which independently revalidates both again.
        try:
            snapshot = bot.build_signal_snapshot(
                inst, side, signal_close_ms, rank
            )
            if not volume_snapshot_passes(snapshot, side):
                raise ValueError(
                    "Mandatory >110 strategy entry condition failed"
                )
            bot.checked_preorder_quote(inst, side, snapshot["close"])
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            log_attempt(candidate, "PRE_ORDER_REJECTED", reason, priority)
            bot.append_technical_event(
                "NEAREST_VOLUME_FALLBACK_PREORDER",
                f"{inst} {side} skipped; trying next volume-eligible coin: {reason}",
                inst=inst, side=side, rank=rank,
                signal_close_ms=signal_close_ms,
                extra={"priority": priority},
            )
            continue

        # A new order must not be attempted after POST /trade/order begins;
        # even a timeout can mean BloFin accepted the order.
        state["_nearest_volume_order_post_started"] = False
        try:
            pos = bot.place_live_trade_multi(
                state,
                candidate,
                tickers,
                instruments,
                budget,  # do not divide 10 USDT by number of fallback options
                f"NEAREST_VOLUME priority {priority}; one order per scan; "
                f"bankroll {live_bankroll:.4f} USDT",
            )
        except Exception as exc:
            uncertain_order = bool(
                state.get("_nearest_volume_order_post_started")
            )
            reason = f"{type(exc).__name__}: {exc}"
            status = (
                "ORDER_STATUS_UNCERTAIN_STOP"
                if uncertain_order else "PRE_ORDER_REJECTED"
            )
            log_attempt(candidate, status, reason, priority)
            bot.append_technical_event(
                "NEAREST_VOLUME_FALLBACK_STOP" if uncertain_order
                else "NEAREST_VOLUME_FALLBACK_PREORDER",
                f"{inst} {side}: {status}: {reason}",
                inst=inst, side=side, rank=rank,
                signal_close_ms=signal_close_ms,
                extra={"priority": priority},
            )
            if uncertain_order:
                break
            continue
        finally:
            # The flag never persists to JSON; outcome is in entry_attempts.
            state.pop("_nearest_volume_order_post_started", None)

        if not pos:
            log_attempt(
                candidate, "ORDER_ATTEMPT_UNCONFIRMED_STOP",
                "Market order was attempted; no safe fallback is possible",
                priority,
            )
            break

        record = log_attempt(candidate, "EXECUTED", "", priority)
        record["notional_usdt"] = pos.get("notional_usdt")
        ranking["executed_candidate"] = {
            "inst": inst, "side": side, "priority": priority,
            "notional_usdt": pos.get("notional_usdt"),
        }
        return [{
            "inst": inst, "side": side, "rank": int(rank),
            "signal_close_ms": int(signal_close_ms),
            "notional_usdt": pos.get("notional_usdt"),
        }]
    if not ranking.get("executed_candidate"):
        print("NEAREST_VOLUME: exhausted safe fallback options; no new order")
    return []
