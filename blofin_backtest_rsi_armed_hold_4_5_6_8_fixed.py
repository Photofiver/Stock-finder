import blofin_backtest_rsi_armed_same_data as base

# Base script's nested get_minutes() passes limit=180, which is only enough for ~3h.
# Override fetch_1m so holds above 3h fetch the full minute window.
def fetch_1m_full(inst, end_ts, limit=180):
    wanted = min(1440, max(180, base.HOLD_HOURS * 60 + 10))
    return base.parse(base.api_get("/api/v1/market/candles", {
        "instId": inst,
        "bar": "1m",
        "after": str(end_ts),
        "limit": str(wanted),
    }))

base.fetch_1m = fetch_1m_full

for hold in [4, 5, 6, 8]:
    base.HOLD_HOURS = hold
    print(f"\n===== HOLD_{hold}H =====")
    base.main()
