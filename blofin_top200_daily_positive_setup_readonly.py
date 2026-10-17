"""Read-only: search a SINGLE identical TOP200 volume entry rule positive every UK day.
Frozen 8 UK days 2026-10-09..16, all 390 current eligible swaps, historical
rolling 24h top200 point-in-time. Use cached OHLCV from workflow 39074940517.
Never places exchange trades or reads private API.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
import itertools
import json
from pathlib import Path
import statistics

import blofin_top200_repeat_setup_research as m
import blofin_top200_parallel_runner as parallel

CAPTURE={}
old=m.compute_daily
def catch(data,outcomes,rule,idx,days):
    if not CAPTURE:
        CAPTURE["data"]=data
        CAPTURE["outcomes"]=outcomes
    return old(data,outcomes,rule,idx,days)

m.compute_daily=catch
parallel.analyze()
m.compute_daily=old
byfam=CAPTURE["data"]
outcomes=CAPTURE["outcomes"]
DAYS=[(parallel.anchor().replace(hour=0)-m.timedelta(days=m.NUM_DAYS-i)).date().isoformat() for i in range(m.NUM_DAYS)]
COST=m.COST_PCT
NOTIONAL=m.NOTIONAL

# Events grouped and sorted before any parameter selection.
buckets=defaultdict(list)
for family,evs in byfam.items():
    for e in evs:
        if e["day"] not in DAYS:
            continue
        h=datetime.fromtimestamp(e["signal_ms"]/1000,m.UK).hour
        x=(e["signal_ms"],e["inst"],e["side"],e["rank"],e["ratio"],e["body"],h)
        buckets[(family,e["day"],e["side"])].append(x)
for fam in m.FAMILY:
    for day in DAYS:
        arr=buckets[(fam,day,"LONG")]+buckets[(fam,day,"SHORT")]
        arr.sort(key=lambda x:(x[0],x[1]))
        buckets[(fam,day,"BOTH")]=arr

RANKS={
    "TOP200":(1,200),
    "TOP100":(1,100),
    "BOTTOM100":(101,200),
    "RANK1_50":(1,50),
    "RANK51_100":(51,100),
    "RANK101_150":(101,150),
    "RANK151_200":(151,200),
}
HOURS={"ALL":(0,24),"UK_00_06":(0,6),"UK_06_12":(6,12),
       "UK_12_18":(12,18),"UK_18_24":(18,24)}
MULTS=(1.0,1.7,2.5,4.0)
BODIES=(0.1,0.25,0.4)
EXITS=m.EXIT_LIST
scanned=0
four_good=[]
six_good=[]
selected=[]
best_train=[]
max_train_positive=0
max_six_positive=0

def evaluate(es,idx):
    busy=0;gross=0;trades=0;netwins=0
    for t,coin,side,rank,ratio,body,hour in es:
        if t<busy:continue
        x=outcomes.get((coin,t,side,idx))
        if x is None:continue
        gain,busy,reason=x
        gross+=gain
        trades+=1
        netwins+=gain>COST
    pnl_pct=gross-COST*trades
    return {"n":trades,"wins_net":netwins,
            "net_usdt":round(pnl_pct*NOTIONAL/100,4),
            "pct_reference":round(pnl_pct,4)}

def picked(fam,side,rank,session,mult,body):
    low,hi=rank
    a,b=session
    return [[x for x in buckets[(fam,day,side)]
             if low<=x[3]<=hi and a<=x[6]<b and x[4]>=mult and x[5]>=body]
            for day in DAYS]

for fam,side,(rname,rank),(tname,hours),vol,body in itertools.product(
        m.FAMILY,("BOTH","LONG","SHORT"),RANKS.items(),HOURS.items(),MULTS,BODIES):
    byday=picked(fam,side,rank,hours,vol,body)
    if any(len(b)<3 for b in byday[:6]):
        continue
    for idx,(tp,sl,hold) in enumerate(EXITS):
        scanned+=1
        tr=[evaluate(byday[i],idx) for i in range(4)]
        successes=sum(d["n"]>=3 and d["net_usdt"]>0 for d in tr)
        max_train_positive=max(max_train_positive,successes)
        if successes<4:
            if len(best_train)<5 or successes>=best_train[-1][0]:
                best_train.append((successes,min(d["net_usdt"] for d in tr),
                                   sum(d["net_usdt"] for d in tr),
                                   (fam,side,rname,tname,vol,body,tp,sl,hold),tr))
                best_train.sort(key=lambda a:(a[0],a[1],a[2]),reverse=True)
                del best_train[5:]
            continue
        desc={"family":fam,"side":side,"rank":rname,"session":tname,
              "vol_vs_median20":vol,"body_min_pct":body,
              "tp_pct":100*tp,"sl_pct":100*sl,"max_hold_minutes":5*hold}
        four_good.append(desc)
        val=[evaluate(byday[i],idx) for i in (4,5)]
        six_matches=sum(d["n"]>=3 and d["net_usdt"]>0 for d in val)
        max_six_positive=max(max_six_positive,4+six_matches)
        if six_matches<2:continue
        six_good.append({"rule":desc,"train":tr,"validation":val,"idx":idx,
                         "byday":byday,
                         "selection_metric":(min(d["net_usdt"] for d in tr+val),
                                             sum(d["net_usdt"] for d in tr+val))})

# Untouched holdout is evaluated ONLY after rule fixed using first six UK days.
six_good.sort(key=lambda a:a["selection_metric"],reverse=True)
for candidate in six_good[:15]:
    z=[evaluate(candidate["byday"][i],candidate["idx"]) for i in (6,7)]
    selected.append({"rule":candidate["rule"],"train":candidate["train"],
                     "validation":candidate["validation"],"holdout":z,
                     "all_eight_net_positive":all(d["net_usdt"]>0 and d["n"]>=3
                                                 for d in candidate["train"]+candidate["validation"]+z),
                     "net_usdt_eight_days":round(sum(d["net_usdt"] for d in candidate["train"]+candidate["validation"]+z),4)})
    del candidate["byday"]

summary={"dates_uk":DAYS,"symbols":390,"top200_historical_causal":True,
         "volume_peak_reset_at_uk_midnight":True,
         "min_filled_trades_each_day":3,
         "requirement":"Every full UK day must have NET profit >0 after 0.22% round-trip costs; optional 3% daily benchmark.",
         "scenarios_evaluated_after_signal_count_pruning":scanned,
         "configurations_positive_all_first_four":len(four_good),
         "configurations_positive_all_first_six":len(six_good),
         "max_positive_days_of_first_four":max_train_positive,
         "max_positive_days_of_first_six_for_four_day_survivors":max_six_positive,
         "top_selected_without_looking_at_holdout":selected[:8],
         "best_train_if_no_candidate":[{"profitable_days_train":x[0],
                                         "least_profitable_day_usdt":x[1],
                                         "train_total_usdt":x[2],
                                         "rule":x[3],"train":x[4]}
                                        for x in best_train],
         "limitations":"390 instruments listed at time of historical research, reconstructed point-in-time ranking among survivors. No funding, dynamic spread, order book, or intra-bar TP/SL chronology. OHLC conservative SL-first; fixed 20 USDT reference and global one-position. Strategy selected using first six dates only; results on final two are out-of-sample but one holdout test cannot guarantee future performance. This is research only; NO LIVE changes."}
Path("blofin_top200_daily_positive_setup_report.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
print("DAILY_PROFIT_SETUP_RESULT="+json.dumps(summary,ensure_ascii=False,separators=(",",":")),flush=True)
