import blofin_backtest_rsi_armed_same_data as base

# Fixed signal logic and fixed 5h maximum hold. Only TP/SL changes.
base.HOLD_HOURS = 5

# Base script normally asks for only 180 one-minute candles; 5h needs >300.
def fetch_1m_full(inst, end_ts, limit=180):
    wanted = min(1440, max(180, base.HOLD_HOURS * 60 + 10))
    return base.parse(base.api_get("/api/v1/market/candles", {
        "instId": inst,
        "bar": "1m",
        "after": str(end_ts),
        "limit": str(wanted),
    }))

base.fetch_1m = fetch_1m_full

for tp in [0.005, 0.0075, 0.010, 0.0125]:
    for sl in [0.005, 0.0075, 0.010]:
        base.TP_PCT = tp
        base.SL_PCT = sl
        print(f"\n===== TP_{tp*100:.2f}_SL_{sl*100:.2f}_H5 =====")
        base.main()
