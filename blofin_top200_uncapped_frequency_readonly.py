"""READ ONLY exact uncapped frequency of the ONE selected TOP200 setup.

Reuses immutable 390-coin 8-day candle shards and research signal family.
Reports all matching pattern signals by UK day, distinct 5m times, and number
of sequential non-overlapping 20USDT trades without artificial 3/day cap.
"""
import collections
from datetime import datetime
import json
from pathlib import Path

import blofin_top200_repeat_setup_research as core
import blofin_top200_parallel_runner as source

real_compute=core.compute_daily
seen={}

def intercepted(byfam,cache,rule,exitid,days):
    seen["byfam"]=byfam
    seen["cache"]=cache
    return real_compute(byfam,cache,rule,exitid,days)

core.compute_daily=intercepted
source.analyze()
core.compute_daily=real_compute

fam=seen["byfam"]["PULLBACK"]
cache=seen["cache"]
exitid=core.EXIT_LIST.index((0.02,0.01,12))
finaldate=source.anchor().replace(hour=0)
days=[(finaldate-core.timedelta(days=core.NUM_DAYS-i)).date().isoformat()
      for i in range(core.NUM_DAYS)]
summary={}
for day in days:
    rows=sorted((e for e in fam if e["day"]==day
                 and e["ratio"]>=1.7
                 and e["body"]>=0.4),
                key=lambda e:(e["signal_ms"],e["inst"]))
    usable=[e for e in rows if (e["inst"],e["signal_ms"],e["side"],exitid) in cache]
    occupied=0
    serial=[]
    for e in usable:
        if e["signal_ms"]<occupied:continue
        pnl,end,reason=cache[(e["inst"],e["signal_ms"],e["side"],exitid)]
        serial.append((e["inst"],e["signal_ms"],e["side"],pnl,reason))
        occupied=end
    summary[day]={
        "all_matching_signals_across_top200":len(rows),
        "unique_5min_moments":len(set(e["signal_ms"] for e in rows)),
        "actionable_signals_with_simulated_exit":len(usable),
        "serial_trades_without_daily_limit":len(serial),
        "serial_gross_winners":sum(x[3]>0 for x in serial),
        "serial_net_usdt_after_costs":round(sum((x[3]-core.COST_PCT)*core.NOTIONAL/100 for x in serial),4),
        "first_three_uk_times":[datetime.fromtimestamp(x[1]/1000,core.UK).strftime("%H:%M") for x in serial[:3]]
    }
report={
    "rule":"PULLBACK, side LONG or SHORT according to dominant volume regime reset 00:00 UK, prev opposite-color bar, bar volume > previous bar and >=1.7 x median of 20 prior 5m bars, bar body >=0.40%, TOP200 by point-in-time 24h price change. TP2%, SL1%, max hold60min.",
    "daily":summary,
    "observed_days":len(days),
    "all_signals_total":sum(s["all_matching_signals_across_top200"] for s in summary.values()),
    "serial_trades_total":sum(s["serial_trades_without_daily_limit"] for s in summary.values()),
    "minimum_signals_per_day":min(s["all_matching_signals_across_top200"] for s in summary.values()),
    "minimum_serial_entries_per_day":min(s["serial_trades_without_daily_limit"] for s in summary.values()),
    "caution":"Historical 8 completed UK days, 390 live-as-of-now swaps, TOP200 ranking reconstructed on history (not exchange point-in-time delisted universe). Same-moment different coins each count separate pattern. Simulated fills, not actual executed orders. No LIVE changes."
}
Path("blofin_top200_uncapped_frequency_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("UNCAPPED_FREQUENCY="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
