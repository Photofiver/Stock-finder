"""Count EVERY raw occurrence of the frozen SHORT four-green-candle pattern.

From three preexisting BloFin public OHLCV datasets. No LIVE changes.
The exact EXHAUST3_FADE conditions are taken unchanged from previous code:
four consecutive green 5m candles ending in the signal candle; latest
5m volume >=2.5 times the median of 20 prior closed bars; candle body
>=0.5%; the coin is in reconstructed point-in-time TOP200 by rolling
24h price gain; subsequent hypothetical entry direction SHORT.
Counting the raw qualifying signal events is not counting profitable
or feasible simultaneous executions (actual strategy takes 3/day).
"""
import collections
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path

import blofin_top200_new_volume_reversals_readonly as rev

PERIODS=[
  ("prior","2026-10-01","2026-10-09","2026-10-09",None),
  ("recent","2026-10-09","2026-10-17","2026-10-17",None),
  ("forward","2026-10-10","2026-10-18","2026-10-18","2026-10-17"),
]
all_daily={}
market_coverage={}
for directory, start, end, asof, only_day in PERIODS:
    market={}
    missing={}
    for part in range(4):
        path=Path(directory)/f"shard_{part}.json.gz"
        with gzip.open(path,"rt",encoding="utf-8") as f:
            obj=json.load(f)
        if obj.get("asof_uk")!=asof:
            raise RuntimeError("Wrong history shard period")
        missing.update(obj.get("errors",{}))
        for inst,rows in obj["candle_rows"].items():
            if inst in market:
                raise RuntimeError("Duplicate instrument")
            market[inst]={int(t):tuple(float(x) for x in bar)
                          for t,bar in rows.items()}
    rev.START=int(datetime.fromisoformat(start+"T00:00:00").replace(tzinfo=rev.UK).timestamp()*1000)
    rev.END=int(datetime.fromisoformat(end+"T00:00:00").replace(tzinfo=rev.UK).timestamp()*1000)
    all_events,counts=rev.events_from_history(market)
    matching=collections.defaultdict(list)
    for (day, window),events in all_events["EXHAUST3_FADE"].items():
        if only_day is not None and day!=only_day:
            continue
        for e in events:
            # e: signal_ts, inst, sign, ratio, body_pct, rank, day, nextopen
            if e[2]==-1 and e[3]>=2.5 and e[4]>=0.5:
                matching[day].append(e)
    begin=datetime.fromisoformat(start+"T00:00:00").replace(tzinfo=rev.UK)
    period_dates=[(begin+timedelta(days=i)).date().isoformat() for i in range(8)]
    if only_day:
        period_dates=[only_day]
    for day in period_dates:
        if day in all_daily:
            raise RuntimeError("Overlapping counting period")
        ev=matching[day]
        hours=[len([e for e in ev if (e[0]//1000) >=0 and (datetime.fromtimestamp(e[0]/1000,rev.UK).hour//8)==s]) for s in range(3)]
        all_daily[day]={
           "signals":len(ev),
           "distinct_5min_entry_times":len(set(e[0] for e in ev)),
           "distinct_coins":len(set(e[1] for e in ev)),
           "signals_by_uk_session":{"00-08":hours[0],
                                   "08-16":hours[1],
                                   "16-24":hours[2]},
           "signals_on_ranked_places_1_50":sum(e[5]<=50 for e in ev),
           "signals_on_ranked_places_51_100":sum(50<e[5]<=100 for e in ev),
           "signals_on_ranked_places_101_200":sum(e[5]>100 for e in ev),
        }
    market_coverage[directory]={"present_currently_listed_coins":len(market),
                                "missing_coins":missing,
                                "raw_all_exhaustion_signals_in_period":counts.get("EXHAUST3_FADE",0)}
    print(f"PERIOD_SIGNAL_COUNT={directory}:{sum(len(matching[d]) for d in period_dates)}",flush=True)
days=sorted(all_daily)
if len(days)!=17 or days[0]!="2026-10-01" or days[-1]!="2026-10-17":
    raise RuntimeError(f"Unexpected days {days}")
total=sum(all_daily[d]["signals"] for d in days)
report={
 "research_question":"How often did the exact previously favored SHORT four-green-candle setup appear across point-in-time TOP200 among currently live BloFin USDT crypto swaps?",
 "frozen_rule":{
    "direction":"SHORT after 4 consecutive green 5m candles",
    "current_volume_vs_20_prior_median_min":2.5,
    "last_green_candle_body_pct_min":0.5,
    "symbol_rank":"1..200 by rolling last 24h gain computed only from historical 5m candles",
    "candles_confirmed":True,
 },
 "period_first_day_uk":days[0],
 "period_last_day_uk":days[-1],
 "days_count":len(days),
 "total_raw_signals":total,
 "average_signals_per_24h":round(total/len(days),2),
 "minimum_signals_per_day":min(x["signals"] for x in all_daily.values()),
 "maximum_signals_per_day":max(x["signals"] for x in all_daily.values()),
 "days_with_at_least_three_raw_signals":sum(x["signals"]>=3 for x in all_daily.values()),
 "total_distinct_5min_entry_times_across_days":sum(x["distinct_5min_entry_times"] for x in all_daily.values()),
 "daily":all_daily,
 "data_coverage":market_coverage,
 "limitations":"Signal count is NOT the executed number nor proven profitable. One max 3/day strategy selects one first qualifying timestamp in each UK 8h window; several symbols can signal simultaneously. Ranks calculated among currently listed symbols: no historical delisted coin universe. Early 8d 388/390 instruments, later 390/390. No private BloFin API or LIVE changes."
}
path=Path("blofin_top200_exhaust_short_17d_frequency_report.json")
path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("EXHAUST_SHORT_17DAY_FREQUENCY="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
