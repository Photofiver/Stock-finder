"""READ ONLY: at most THREE daily entries of ONE recurring volume setup.

Reuses the previous 390-coin point-in-time historical TOP200 candle shards,
same 8 full UK days / fee assumptions. Causal selection alternatives:
(A) FIRST_THREE: execute first 3 eligible setups after previous exits, then
    stop for that calendar day.
(B) THREE_SHIFTS: execute first setup in each local UK 00-08, 08-16, 16-24
    window, at most 3 daily.
No foreknowledge: signals chosen only when closed 5m bar observed.
Train first 4 days, validate next 2, then frozen holdout final 2 days.
No LIVE changes, no broker credentials or order placement.
"""
import collections
from datetime import datetime
import itertools
import json
from pathlib import Path

import blofin_top200_repeat_setup_research as orig
import blofin_top200_parallel_runner as source

ORIGINAL_COMPUTE=orig.compute_daily
SAVED={}

def trap(byfam,cache,rule,exitid,days):
    SAVED["byfam"]=byfam
    SAVED["cache"]=cache
    return ORIGINAL_COMPUTE(byfam,cache,rule,exitid,days)

orig.compute_daily=trap
source.analyze()
orig.compute_daily=ORIGINAL_COMPUTE

byfam=SAVED["byfam"]
cache=SAVED["cache"]

base=source.anchor().replace(hour=0)
days=[(base-orig.timedelta(days=orig.NUM_DAYS-i)).date().isoformat()
      for i in range(orig.NUM_DAYS)]
train_days=days[:4]
val_days=days[4:6]
hold_days=days[6:8]


def limited(rule,exitid,evaldays,mode):
    family,mult,body,side=rule
    events=[e for e in byfam.get(family,[])
            if e["ratio"]>=mult and e["body"]>=body
            and (side=="BOTH" or e["side"]==side)
            and e["day"] in evaldays]
    events.sort(key=lambda e:(e["signal_ms"],e["inst"]))
    chosen={d:[] for d in evaldays}
    used_windows={d:set() for d in evaldays}
    busy=-1
    for e in events:
        day=e["day"]
        if e["signal_ms"]<busy or len(chosen[day])>=3:
            continue
        local=datetime.fromtimestamp(e["signal_ms"]/1000,orig.UK)
        window=local.hour//8
        if mode=="ONE_PER_8H_WINDOW" and window in used_windows[day]:
            continue
        outcome=cache.get((e["inst"],e["signal_ms"],e["side"],exitid))
        if not outcome:continue
        pct,end,reason=outcome
        chosen[day].append((pct,reason,e["inst"],e["signal_ms"]))
        busy=end
        used_windows[day].add(window)
    return {day:{
                "trades":len(entries),
                "wins_net":sum(p>orig.COST_PCT for p,_,_,_ in entries),
                "profit_usdt":round(sum(p-orig.COST_PCT for p,_,_,_ in entries)*orig.NOTIONAL/100,4),
                "return_on_20usdt_pct":round(sum(p-orig.COST_PCT for p,_,_,_ in entries),4),
                "entry_uk_times":[datetime.fromtimestamp(ts/1000,orig.UK).strftime("%H:%M") for _,_,_,ts in entries],
                "symbols":[x[2] for x in entries]}
            for day,entries in chosen.items()}

modes=("FIRST_THREE","ONE_PER_8H_WINDOW")
summary={}
for mode in modes:
    ranks=[]
    meets_train_goal=0
    three_daily_train=0
    for rule in itertools.product(orig.FAMILY,orig.VOL_MULT,orig.BODY_MIN,orig.SIDES):
        for exitid,cfg in enumerate(orig.EXIT_LIST):
            p=orig.assess(limited(rule,exitid,train_days,mode))
            if p["min_trades_in_day"]<3:continue
            three_daily_train+=1
            if p["avg_net_usdt_per_day"]>=orig.GOAL_USDT:
                meets_train_goal+=1
            ranks.append((rule,exitid,p))
    # Require first train all 3; rank by days achieving +3 and daily average net.
    ranks.sort(key=lambda r:(r[2]["days_at_least_3pct"],r[2]["avg_net_usdt_per_day"],
                             r[2]["min_day_net_usdt"]),reverse=True)
    verified=[]
    for rule,idx,tr in ranks:
        valid=orig.assess(limited(rule,idx,val_days,mode))
        if valid["min_trades_in_day"]!=3:continue
        if tr["avg_net_usdt_per_day"] <orig.GOAL_USDT:continue
        if valid["avg_net_usdt_per_day"] <orig.GOAL_USDT:continue
        verified.append((rule,idx,tr,valid))
    verified.sort(key=lambda x:(x[3]["days_at_least_3pct"],x[2]["days_at_least_3pct"],
                                x[3]["avg_net_usdt_per_day"]),reverse=True)

    picked=verified[:3] if verified else [(rule,idx,tr,orig.assess(limited(rule,idx,val_days,mode))) for rule,idx,tr in ranks[:3]]
    described=[]
    for rule,idx,tr,val in picked:
        hold=orig.assess(limited(rule,idx,hold_days,mode))
        tp,sl,minutes=orig.EXIT_LIST[idx]
        desc={
            "family":rule[0],"volume_threshold_median20":rule[1],
            "minimum_body_pct":rule[2],"direction":rule[3],
            "TP_pct":tp*100,"SL_pct":sl*100,"max_hold_minutes":minutes*5,
            "train":tr,"validation":val,"untouched_holdout":hold,
            "meets_3trades_every_day":tr["min_trades_in_day"]==3 and val["min_trades_in_day"]==3 and hold["min_trades_in_day"]==3,
            "meets_3pct_every_day":tr["days_at_least_3pct"]==4 and val["days_at_least_3pct"]==2 and hold["days_at_least_3pct"]==2,
            "average_net_8d_usdt":round((tr["total_net_usdt"]+val["total_net_usdt"]+hold["total_net_usdt"])/8,4),
            "total_net_8d_usdt":round(tr["total_net_usdt"]+val["total_net_usdt"]+hold["total_net_usdt"],4)
        }
        described.append(desc)
    summary[mode]={
        "tested":len(orig.FAMILY)*len(orig.VOL_MULT)*len(orig.BODY_MIN)*len(orig.SIDES)*len(orig.EXIT_LIST),
        "configs_three_fills_all_four_train_days":three_daily_train,
        "configs_three_daily_train_and_average_3pct_train":meets_train_goal,
        "configs_meeting_3pct_average_both_train_and_validation":len(verified),
        "best_preselected_frozen_on_train_validation":described}

result={
    "historical_uk_days":days,
    "executions":"FIRST_THREE or at most one first closed-signal entry each of 3 non-overlapping UK 8h windows. One actual 20USDT position globally, no contemporaneous positions.",
    "same_one_volume_setup":"Dominant-volume regime reset every UK midnight; volume multiplier over previous20 median; bar body; one chosen direction; next 5m open entry; fixed TP/SL/time.",
    "cost_roundtrip_pct":orig.COST_PCT,
    "nominal_usdt":orig.NOTIONAL,
    "three_daily_profit_goal_usdt":orig.GOAL_USDT,
    "mode_results":summary,
    "notes":"Historical top200 recreated from 390 present live swaps at each 5m close by rolling24h price change. Ex-post universe, no funding. 8-day test includes 4-day train, 2-day validation, 2-day untouched holdout. Selection from training and validation only. Do not deploy LIVE based on past results."
}
Path("blofin_top200_exactly_three_trades_report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("EXACTLY_THREE_PER_DAY_RESULT="+json.dumps(result,ensure_ascii=False,separators=(",",":")),flush=True)
