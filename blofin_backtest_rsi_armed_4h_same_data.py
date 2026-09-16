from blofin_backtest_rsi_armed_15_30_45_same_data import (
    SYMBOLS, START_TS, END_TS, HOLD_MIN, D1M, D5M,
    fetch_paginated, simulate, fmt,
)


def main():
    # 4H needs a longer warm-up for RSI/Stochastic and min_idx=30.
    warmup_start = START_TS - 8 * 24 * 60 * D1M
    exit_end = END_TS + HOLD_MIN * D1M
    source_5m = {}
    source_1m = {}

    for inst in SYMBOLS:
        source_5m[inst] = fetch_paginated(inst, "5m", D5M, warmup_start, END_TS, max_pages=8)
        source_1m[inst] = fetch_paginated(inst, "1m", D1M, START_TS, exit_end, max_pages=10)
        print(f"FETCHED {inst} 5m={len(source_5m[inst])} 1m={len(source_1m[inst])}")

    print("FROZEN_DATA=true")
    print("SYMBOLS=" + ",".join(SYMBOLS))
    print(f"WINDOW={fmt(START_TS)} -> {fmt(END_TS)} UTC")
    print("COMMON=RSI14 cross 50 arms coin; wait for same-direction Stochastic 14,3,3 cross; candle color agrees; volume>previous and <=2.5x median(prev3); one entry per coin until next RSI cross; one global position; TP0.5%; SL1%; max2h; 1m exit ordering")
    print("SIGNAL_BAR=4H aggregated from the same frozen 5m candle source; longer pre-window warmup used only for indicators")

    r = simulate(240, source_5m, source_1m)
    print(
        f"RESULT_4H: trades={r['trades']} wins={r['wins']} losses={r['losses']} "
        f"time={r['time']} same1m={r['same']} win_all={r['win_all']:.2f}% "
        f"win_tp_sl={r['win_tp_sl']:.2f}% pnl={r['pnl']:+.4f} USDT "
        f"rsi_crosses={r['rsi_crosses']} raw_candidate_times={r['raw_candidates']} "
        f"blocked_same_coin_checks={r['blocked']}"
    )
    print("fees_slippage_excluded=true")


if __name__ == "__main__":
    main()
