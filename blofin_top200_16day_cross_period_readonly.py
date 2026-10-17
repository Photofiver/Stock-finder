"""READ ONLY: cross-period search for a single repeated TOP200 volume setup.

16 UK days 2026-10-01..16 across 390 currently listed BloFin instruments,
with first 8 days from the frozen-validation job and last 8 days from the
original discovery job. Pre-existing market data only. No trade APIs.

CAVEAT: original 9-16 period was used for earlier strategy discovery;
thus the whole 16-day analysis is exploratory and needs a truly fresh forward
day to substantiate any top strategy. Scan exact same parameter universe as
the previous reversal study, without cherry-picking individual signal outcome.
"""
import collections
import gzip
import json
from datetime import datetime,timedelta
from pathlib import Path

import blofin_top200_new_volume_reversals_readonly as rev

PERIODS=[
 ("prior","2026-10-01","2026-10-09","2026-10-09"),
 ("recent","2026-10-09","2026-10-17","2026-10-17")
]
family_events=collections.defaultdict(dict)
all_market=collections.defaultdict(dict)
all_days=[]

for folder,start,end,asof in PERIODS:
    startday=datetime.fromisoformat(start+"T00:00:00").replace(tzinfo=rev.UK)
    endday=datetime.fromisoformat(end+"T00:00:00").replace(tzinfo=rev.UK)
    rev.START=int(startday.timestamp()*1000)
    rev.END=int(endday.timestamp()*1000)
    dataset={}
    missing={}
    for shard in range(4):
        path=Path(folder)/f"shard_{shard}.json.gz"
        with gzip.open(path,"rt",encoding="utf-8") as f:part=json.load(f)
        if part["asof_uk"]!=asof:
            raise RuntimeError("Wrong market data period")
        for inst,bars in part["candle_rows"].items():
            dataset[inst]={int(t):tuple(map(float,b)) for t,b in bars.items()}
        missing.update(part["errors"])
    if len(dataset)<350:raise RuntimeError("Insufficient prior/current coin coverage")
    evs,counts=rev.events_from_history(dataset)
    for fam,slots in evs.items():
        for date_slot,rows in slots.items():
            if date_slot in family_events[fam]:
                raise RuntimeError("Duplicate day/slot")
            family_events[fam][date_slot]=rows
    for inst,bars in dataset.items():
        all_market[inst].update(bars)
    all_days.extend((startday+timedelta(days=i)).date().isoformat() for i in range(8))
    print(f"PERIOD_READY={folder},COINS={len(dataset)},DAYS={start}..{end},MISSING={len(missing)}",flush=True)
rev.DATA=dict(all_market)
rev.EVENTS=family_events
rev.OUTCOME_CACHE.clear()

train=all_days[:8]
validate=all_days[8:14]
stress=all_days[14:16]
scanned=0
freq=0
profitable8=0
positive8=0
candidates=[]
for fam in rev.FAMILIES:
    for vm in rev.VOL_THRESHOLDS:
        for bm in rev.BODY_THRESHOLDS:
            for side in (0,1,-1):
                for tie in rev.SELECTORS:
                    for exitid in range(len(rev.EXITS)):
                        scanned+=1
                        cfg=(fam,vm,bm,side,tie,exitid)
                        days=rev.eval_config(cfg,train)
                        if any(x["trades"]!=3 for x in days):continue
                        freq+=1
                        summary=rev.calc_score(days)
                        if summary["net_total_usdt"]>0:profitable8+=1
                        if summary["positive_days"]==8:positive8+=1
                        if summary["positive_days"]>=6:
                            candidates.append((cfg,summary))
print(f"SEARCH16_TRAIN_PROGRESS=TOTAL={scanned},FREQUENT={freq},8D_PROFIT={profitable8},8D_ALL_POSITIVE={positive8},6PLUS={len(candidates)}",flush=True)

ranked=[]
for conf,tr in candidates:
    vv=rev.calc_score(rev.eval_config(conf,validate))
    if vv["three_entry_days"]<6:continue
    ranked.append((conf,tr,vv))
# Frozen candidate choice uses 8 training + 6 validation only. Later days are
# descriptive stress-test because they were seen in previous research.
ranked.sort(key=lambda t:(t[2]["positive_days"],t[1]["positive_days"],
                          t[2]["days_at_least_3pct"],t[1]["days_at_least_3pct"],
                          t[2]["avg_daily_usdt"],t[1]["avg_daily_usdt"]),reverse=True)
summary=[]
for c,tr,val in ranked[:12]:
    z=rev.calc_score(rev.eval_config(c,stress,need_detailed=True))
    fam,vm,bm,side,tie,idx=c
    summary.append({"setup":{"family":fam,"vol_multiple_min":vm,"body_pct_min":bm,
                              "side":"SHORT" if side<0 else "LONG" if side>0 else "BOTH",
                              "selection":tie,"tp_pct":100*rev.EXITS[idx][0],
                              "sl_pct":100*rev.EXITS[idx][1],
                              "max_hold_minutes":5*rev.EXITS[idx][2]},
                    "training_oct1_8":tr,"validation_oct9_14":val,
                    "last2_descriptive_stress_oct15_16":z,
                    "positive_all_16_days":tr["positive_days"]==8 and val["positive_days"]==6 and z["positive_days"]==2,
                    "net_16days":round(tr["net_total_usdt"]+val["net_total_usdt"]+z["net_total_usdt"],4)})
report={
    "periods":PERIODS,
    "total_eligible":len(all_market),
    "scenarios":scanned,
    "train8_three_trades_every_day":freq,
    "train8_profitable_total":profitable8,
    "train8_each_day_profitable":positive8,
    "train8_at_least_6_positive_days":len(candidates),
    "tested_next6_after_train_filter":len(ranked),
    "top_preselected_stress_checked":summary,
    "warning":"Not an untouched 16d forward test: 9-16 October was previously used for many strategy searches; this new 16d scan is exploratory and requires genuinely NEW forward period. Real public candles, assumed 0.22% roundtrip costs, current-listed coin survivorship bias, excludes funding/orderbook/precise intrabar fills. Read-only, LIVE unchanged."
}
Path("blofin_top200_16day_cross_period_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("CROSS_PERIOD_16D_RESULT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
