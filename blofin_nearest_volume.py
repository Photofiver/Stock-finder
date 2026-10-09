"""LIVE 15m TOP7 nearest-score selection, requiring directional volume.

Scores match the 10 SHORT and 7 LONG conditions saved in live_watch_history.
This module only selects candidates; actual broker safety/order code is unchanged.
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
    """The same 10 SHORT and 7 LONG checks as in saved scan diagnostics."""
    if i < 20:
        return []
    s = bot.short_entry_metrics(bars, i)
    l = bot.long_entry_metrics(bars, i)
    if not s or not l:
        return []
    current, previous = bars[i], bars[i - 1]
    close_previous = float(previous["c"])
    rise_1bar = (
        (float(current["c"]) / close_previous - 1.0) * 100
        if close_previous > 0 else None
    )
    bullish_pattern = any(
        str(p.get("bias") or "").upper() == "LONG"
        and float(p.get("confidence") or 0) >=
        bot.SHORT_OPPOSING_BULL_PATTERN_MIN_CONFIDENCE
        for p in bot.detect_chart_patterns(bars, i)
    )
    checks = {
        "SHORT": [
            ("RED", s["red_candle"]),
            ("RED_VOL_GT_GREEN", s["volume_higher_than_last_green"]),
            ("BODY_LE_1PCT", s["short_body_max_1pct"]),
            ("LOWER_WICK_LE_50PCT", s["lower_wick_max_50pct"]),
            ("RETURN4_GE_MINUS_2PCT", s["return_4_bars_min_minus_2pct"]),
            ("RETURN4_LE_1PCT", s["return_4_bars_max_1pct"]),
            ("RETURN20_LE_10PCT", s["return_20_bars_max_10pct"]),
            ("RSI_GE_55", s["short_rsi_min_55"]),
            ("MACD_HIST_DELTA_PCT_CLOSE_IN_RANGE", s["short_macd_hist_delta_ok"]),
            ("NO_BULL_PATTERN_GE_70PCT", not bullish_pattern),
        ],
        "LONG": [
            ("GREEN", l["green_candle"]),
            ("GREEN_VOL_GT_RED", l["volume_higher_than_last_red"]),
            ("BODY_GE_60PCT", l["green_body_min_60pct"]),
            ("RISE10_GE_2PCT", l["rise_10_bars_min_2pct"]),
            ("STOCH_NO_DOWN_LAST3", l["stoch_long_ok"]),
            ("RSI_LT_67", l["rsi_below_67"]),
            ("ONE_BAR_RISE_LT_3PCT", rise_1bar is not None
             and rise_1bar < bot.LONG_MAX_1BAR_RISE_PCT),
        ],
    }
    options = []
    color = bot.candle_color(current)
    for side in ("SHORT", "LONG"):
        tests = checks[side]
        missing = [name for name, passed in tests if not passed]
        ref = s["last_green_volume"] if side == "SHORT" else l["last_red_volume"]
        advantage = (
            (float(current["v"]) / float(ref) - 1.0) * 100
            if ref not in (None, 0) else None
        )
        correct_color = color == ("GREEN" if side == "LONG" else "RED")
        volume_pass = (
            s["volume_higher_than_last_green"] if side == "SHORT"
            else l["volume_higher_than_last_red"]
        )
        passed_count = len(tests) - len(missing)
        options.append({
            "side": side,
            "score": f"{passed_count}/{len(tests)}",
            "score_ratio": passed_count / len(tests),
            "missing": missing,
            "missing_count": len(missing),
            "volume_rule_passed": bool(
                correct_color and volume_pass
                and advantage is not None and advantage > 0
            ),
            "volume_advantage_pct": advantage,
        })
    return options


def volume_snapshot_passes(snapshot, side):
    if side not in ("LONG", "SHORT") or snapshot.get("snapshot_error"):
        return False
    metrics = snapshot.get("filter_metrics") or {}
    correct_color = snapshot.get("color") == (
        "GREEN" if side == "LONG" else "RED"
    )
    key = (
        "volume_higher_than_last_red" if side == "LONG"
        else "volume_higher_than_last_green"
    )
    return (
        correct_color
        and bool(metrics.get(key))
        and float(snapshot.get("volume_vs_opposite_ratio") or 0) > 1
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
                raise ValueError("Insufficient confirmed 15m bars")
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
            print(f"NEAREST_VOLUME data unavailable {inst}: {exc}")
    ordered = sorted(
        [row for row in options
         if row["volume_rule_passed"]
         and 0 <= row["signal_age_ms"] <= bot.SIGNAL_MAX_AGE_MS],
        key=lambda row: (
            row["missing_count"], -row["score_ratio"],
            -row["volume_advantage_pct"], row["rank_in_top7"],
            row["inst"], row["side"],
        ),
    )
    state["last_pretrade_ranking"] = {
        "generated_at_ms": bot.now_ms(),
        "strategy": "NEAREST_VOLUME",
        "volume_is_mandatory": True,
        "max_new_positions_per_scan": 1,
        "fallback_after_preorder_rejection": True,
        "not_a_profit_prediction": True,
        "scores": sorted(
            options,
            key=lambda row: (
                row["missing_count"], -row["score_ratio"], row["rank_in_top7"],
            ),
        ),
        "errors": errors,
        "eligible_entry_order": [{
            "inst": row["inst"], "side": row["side"],
            "top7_rank": row["rank_in_top7"],
            "score": row["score"],
            "missing": row["missing"],
            "volume_advantage_pct": row["volume_advantage_pct"],
            "priority": idx,
        } for idx, row in enumerate(ordered, start=1)],
        "entry_attempts": [],
    }
    if not ordered:
        print("NEAREST_VOLUME no entry: no fresh volume-confirmed TOP7 candidate")
        return []
    print(
        "NEAREST_VOLUME fallback order: " +
        ", ".join(
            f"{row['inst']} {row['side']} {row['score']}"
            for row in ordered
        )
    )
    return [
        (row["rank_in_top7"], row["inst"], row["side"], row["signal_close_ms"])
        for row in ordered
    ]
