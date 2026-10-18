"""READ ONLY: fee-free comparison of current exit vs 3 consecutive same-colour bars.
24h (Oct17-18) and independent 8 days (Oct9-17) frozen public candle archives.
Do not place orders, request private endpoints, or modify LIVE strategy.
"""
import json
from pathlib import Path
import short60_volume_mean10_24hour_readonly as frozen
import research_three_same_colour_exit_top200_24h_readonly as one
import research_three_same_colour_exit_top200_8d_readonly as eight
import research_compare_flip24_early_same_top200_readonly as archive

CASES=("CURRENT","AFTER_3_CONSECUTIVE")

def main():
    one.FEES=0.
    eight.COST=0.
    market,errors,total=frozen.load()
    if errors or len(market)!=390:raise RuntimeError("Incomplete 24h market archive")
    ranks,coverage=frozen.rankings(market)
    d1={case:one.portfolio(market,ranks,case) for case in CASES}
    historic=archive.load()
    if len(historic)!=390:raise RuntimeError("Incomplete 8-day archive")
    rankings=eight.rankings(historic)
    d8={case:eight.simulate(historic,rankings,case) for case in CASES}
    report={
        "study":"READ ONLY -- Zero commissions, zero slippage, no funding, simulated next-open fills",
        "capital_USDT":25,
        "leverage":"1x, maximum 4 positions",
        "top200":"point-in-time rankings every closed 5m candle",
        "entry":"consecutive opposite-colour flip with both volumes <= separate previous 20 avg",
        "current_exit":"first opposite closed 5m candle volume strictly > volume of opening signal candle",
        "3_in_a_row_exit":"3 consecutive 5m same-colour candles INCLUDING original signal bar",
        "24h":{"period_UK":"2026-10-17 18:00 to 2026-10-18 18:00", "variants":d1},
        "8d":{"period_UK":"2026-10-09 00:00 to 2026-10-17 00:00", "variants":d8}
    }
    Path("no_fees_current_vs_3_consecutive_24h_8d.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    summary={}
    for key,variants in (("24h",d1),("8d",d8)):
        summary[key]={}
        for case,x in variants.items():
            if key=="24h":
                m=x["metrics"]["total_including_end_mark_to_market"]
                summary[key][case]={"trades":x["entries"],"win_rate_percent":m["win_pct"],"gross_total_USDT":m["sum_usdt"],"return_percent_on_25_USDT":m["portfolio_return_pct_on_25_USDT"],"open_end":x["period_end_open"]}
            else:
                summary[key][case]={"trades":x["entries"],"win_rate_percent":x["trades"]["win_pct"],"gross_total_USDT":x["net_USDT"],"return_percent_on_25_USDT":x["total_return_pct_on_25"],"profit_days":x["profit_days"],"open_end":x["positions_open_at_end"],"days":x["daily"]}
    print("NO_FEES_COMPARE="+json.dumps(summary,ensure_ascii=False,separators=(",",":")),flush=True)

if __name__=="__main__":
    main()
