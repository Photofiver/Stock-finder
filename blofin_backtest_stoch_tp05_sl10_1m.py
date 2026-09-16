import blofin_backtest_stoch_tp05_sl05_1m as base

base.SL_PCT = 0.01

if __name__ == "__main__":
    print("OVERRIDE: TP=0.5% SL=1.0% HOLD=2H; 1m used only to resolve exit order")
    base.main()
