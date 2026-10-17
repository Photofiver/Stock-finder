"""READ ONLY: frozen candidate validation on 8 unseen earlier completed UK days.

No fitting or retuning. Run after blofin_top200_parallel_runner.py fetch 0..3
shards generated with RESEARCH_ASOF_UK=2026-10-09. This checks whether the
TOP200 three-entry upper-breakout SHORT fade found on 2026-10-09..16 works
in 2026-10-01..08. Uses independently computed as-of-time ranks, no future
candles for signals, and the same fills/costs as discovery engine.
"""
import gzip
import json
from pathlib import Path
from datetime import datetime,timedelta,timezone

import blofin_top200_new_volume_reversals_readonly as rev

ASOF="2026-10-09"
STARTDAY=datetime(2026,10,1,tzinfo=rev.UK)
ENDDAY=datetime(2026,10,9,tzinfo=rev.UK)
rev.T0=STARTDAY
rev.T1=ENDDAY
rev.START=int(STARTDAY.timestamp()*1000)
rev.END=int(ENDDAY.timestamp()*1000)
rev.DAYNAMES=[(STARTDAY+timedelta(days=i)).date().isoformat() for i in range(8)]

market={}
missing={}
declared=[]
for s in range(4):
    path=Path("shards")/f"shard_{s}.json.gz"
    with gzip.open(path,"rt",encoding="utf-8") as fh:
        part=json.load(fh)
    if part["asof_uk"]!=ASOF:raise RuntimeError("Wrong or mixed historical source files")
    declared.extend(part["symbols_in_shard"])
    missing.update(part["errors"])
    for inst,records in part["candle_rows"].items():
        if inst in market:raise RuntimeError("Duplicate")
        market[inst]={int(t):tuple(float(x) for x in b) for t,b in records.items()}
print("PRIOR_MARKETS="+str(len(market))+" DECLARED="+str(len(set(declared)))+" MISSING="+str(len(missing)),flush=True)
if len(market)<350 or len(missing)>30:raise RuntimeError("Insufficient independent history")
rev.DATA=market
rev.EVENTS,counts=rev.events_from_history(market)
rule=("BREAKOUT_FADE",2.5,0.5,-1,"HIGHEST_VOL_RATIO",rev.EXITS.index((.03,.01,24)))
perday=rev.eval_config(rule,rev.DAYNAMES,need_detailed=True)
stats=rev.calc_score(perday)
result={
    "source":"Real BloFin closed 5-minute market candles, historical TOP200 rank re-created from rolling 24h price change, current-listed instrument universe. Oct1-8 FULL days UK.",
    "selection":"FROZEN 2026-10-17 parameter set; NO search or new thresholds on Oct1-8",
    "period":rev.DAYNAMES,
    "symbols":len(market),
    "missing_symbols":missing,
    "candidate":{"family":"BREAKOUT_FADE","vol_vs_median20_min":2.5,
        "body_min_pct":0.5,"direction":"SHORT",
        "tie_break":"HIGHEST_VOL_RATIO",
        "TP_pct":3,"SL_pct":1,"max_hold_minutes":120,
        "one_earliest_signal_in_each_uk_window":["00-08","08-16","16-24"]},
    "cost_round_trip_pct":.22,"trade_exposure_usdt":20.,
    "results":stats,
    "pass_all_eight_days_profitable":stats["positive_days"]==8,
    "pass_all_eight_days_3pct":stats["days_at_least_3pct"]==8,
    "caveats":"Exchange public OHLC 5m; approximate fill; no depth/funding/liquidation/delisted coin history; backtest not actual trading. Research only and no LIVE changes."
}
Path("blofin_top200_frozen_short_prior8_report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("FROZEN_PRIOR_EIGHT_RESULT="+json.dumps(result,ensure_ascii=False,separators=(",",":")),flush=True)
