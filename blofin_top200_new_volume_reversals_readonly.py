"""READ ONLY: NEW volume/price reversal setups with 3 causal entries per UK day.

Data: the existing verified 390-current-instrument, 8 full day public BloFin
5m candle shards. Reconstruct TOP200 by 24h close change at each signal bar.
All candle indicators use past or current fully CLOSED 5m candles. Enter on
NEXT candle open. Each of THREE 8-hour UK windows offers the FIRST eligible
signal, choosing the strongest among simultaneously available candidates.
No outcomes used to select within any timestamp. One global 20USDT position.
Train 4d, validate next 2d; last 2d are holdout, evaluated once for strictly
preselected variants. 0.12% fees + adverse 0.1% execution slippage roundtrip.
Not an investment recommendation and not a LIVE bot script.
"""
import collections
import gzip
import itertools
import json
import math
import statistics
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UK=ZoneInfo("Europe/London")
STEP=300000
FIVE=STEP
ASOF="2026-10-17"
T0=datetime(2026,10,9,tzinfo=UK)
T1=datetime(2026,10,17,tzinfo=UK)
START=int(T0.timestamp()*1000)
END=int(T1.timestamp()*1000)
DAYNAMES=[(T0+timedelta(days=j)).date().isoformat() for j in range(8)]
COST=0.22
NOTIONAL=20.0

# We keep volume regime, but also allow testing the inverse hypothesis.
FAMILIES=(
  "VOL_FOLLOW","VOL_FADE","PULLBACK_FOLLOW","PULLBACK_FADE",
  "VOL_FLIP_FOLLOW","VOL_FLIP_FADE","WEAK_COUNTER_TREND",
  "BREAKOUT_FOLLOW","BREAKOUT_FADE","EXHAUST3_FADE",
  "PRICE_SWEEP_REVERSE","COLOR_TURN_FOLLOW","COLOR_TURN_FADE",
)
VOL_THRESHOLDS=(1.5,2.5,4.)
BODY_THRESHOLDS=(0.2,0.5)
SIDE_FILTERS=("BOTH","LONG","SHORT")
SELECTORS=("TOP_RANK","HIGHEST_VOL_RATIO","HIGHEST_BODY","LOWEST_RANKED_24H_GAIN")
EXITS=((0.01,0.005,6),(0.01,0.01,12),
       (0.015,0.005,12),(0.015,0.01,12),
       (0.02,0.005,12),(0.02,0.01,12),
       (0.03,0.01,24),(0.03,0.02,24),
       (0.02,0.01,24),(0.01,0.005,24),
       (0.015,0.01,24),(0.025,0.01,24),
       (0.02,0.01,6),(0.01,0.005,12),
       (0.007,0.005,6),(0.007,0.01,12))
# exit tuple: TP fraction, SL fraction, max number 5m candles
SIGNS={"LONG":1,"SHORT":-1}

def load_data():
    result={}
    errors={}
    symbols=[]
    for shard in range(4):
        path=Path("shards")/f"shard_{shard}.json.gz"
        with gzip.open(path,"rt",encoding="utf-8") as fh:
            d=json.load(fh)
        if d.get("asof_uk")!="2026-10-17":
            raise RuntimeError(f"Wrong dataset period {path}")
        symbols.extend(d["symbols_in_shard"])
        errors.update(d.get("errors",{}))
        for inst,bars in d["candle_rows"].items():
            if inst in result:raise RuntimeError("Duplicate instrument")
            result[inst]={int(k):tuple(float(x) for x in v) for k,v in bars.items()}
    if len(result)!=390 or errors:raise RuntimeError(f"Need same 390 coin complete source, got {len(result)} errors{len(errors)}")
    print("MARKET_ROWS_LOADED="+str(sum(len(x) for x in result.values())),flush=True)
    return result

def median20(xs):
    return statistics.median(xs)

# each event contains causal feature only: signal ts, next entry, price,
# family, side, vol_ratio, body, current rank and coin id.
def events_from_history(data):
    timeline=range(START,END,STEP)
    events=collections.defaultdict(lambda:collections.defaultdict(list))
    time_of_day={}
    # exact day-reset regime for each symbol (no historical warmup carry)
    states={}
    for inst,bars in data.items():
        direction=None
        peak=0
        last_date=None
        state={}
        for ts in timeline:
            b=bars.get(ts)
            if b is None:continue
            d=datetime.fromtimestamp(ts/1000,UK).date()
            if d!=last_date:
                direction=None;peak=0;last_date=d
            current=1 if b[3]>b[0] else -1 if b[3]<b[0] else 0
            flipped=False
            old=direction
            if current and b[4]>0:
                if direction is None:direction=current;peak=b[4]
                elif current==direction:peak=max(peak,b[4])
                elif b[4]>peak:
                    direction=current;peak=b[4];flipped=True
            state[ts]=(direction,flipped,peak,old)
        states[inst]=state

    count=collections.Counter()
    coverage=[]
    for idx,ts in enumerate(timeline):
        r=[]
        prior=ts-288*STEP
        for inst,bars in data.items():
            cur=bars.get(ts)
            old=bars.get(prior)
            if cur is not None and old is not None and old[3]>0:
                r.append((cur[3]/old[3]-1,inst))
        if len(r)<200:raise RuntimeError("Historical TOP200 unavailable")
        coverage.append(len(r))
        r.sort(reverse=True)
        day=datetime.fromtimestamp(ts/1000,UK)
        daykey=day.date().isoformat()
        win=(day.hour//8)
        key=(daykey,win)
        for rank,(_,inst) in enumerate(r[:200],1):
            bars=data[inst]
            b=bars.get(ts)
            cnext=bars.get(ts+STEP)
            if b is None or cnext is None:continue
            if datetime.fromtimestamp((ts+STEP)/1000,UK).date()!=day.date():continue
            o,h,l,c,v=b
            if o<=0 or v<=0:continue
            current=1 if c>o else -1 if c<o else 0
            if not current:continue
            prevs=[bars.get(ts-j*STEP) for j in range(1,21)]
            if any(p is None for p in prevs):continue
            prev=prevs[0]
            med=median20([p[4] for p in prevs])
            if med<=0:continue
            ratio=v/med
            if ratio<min(VOL_THRESHOLDS):continue
            body=abs(c/o-1)*100
            if body<min(BODY_THRESHOLDS):continue
            prevsign=1 if prev[3]>prev[0] else -1 if prev[3]<prev[0] else 0
            volgreater=v>prev[4]
            state=states[inst].get(ts,(None,False,0,None))
            direction,flipped,peak,oldtrend=state
            if direction is None:continue
            prev3=[bars.get(ts-j*STEP) for j in range(1,4)]
            same3=(all(p is not None and (1 if p[3]>p[0] else -1 if p[3]<p[0] else 0)==current for p in prev3))
            prev6=[bars.get(ts-j*STEP) for j in range(1,7)]
            six=all(p is not None for p in prev6)
            breakout=six and ((current==1 and c>max(p[1] for p in prev6))
                              or (current==-1 and c<min(p[2] for p in prev6)))
            sweep_long=six and l<min(p[2] for p in prev6) and c>min(p[2] for p in prev6)
            sweep_short=six and h>max(p[1] for p in prev6) and c<max(p[1] for p in prev6)
            signal_types={}
            if volgreater and direction==current:
                signal_types["VOL_FOLLOW"]=current
            if volgreater:
                signal_types["VOL_FADE"]=-current
            if prevsign==-direction and current==direction and volgreater:
                signal_types["PULLBACK_FOLLOW"]=current
                signal_types["PULLBACK_FADE"]=-current
            if flipped:
                signal_types["VOL_FLIP_FOLLOW"]=current
                signal_types["VOL_FLIP_FADE"]=-current
            if direction!=current and v<peak and volgreater:
                signal_types["WEAK_COUNTER_TREND"]=direction
            if breakout:
                signal_types["BREAKOUT_FOLLOW"]=current
                signal_types["BREAKOUT_FADE"]=-current
            if same3:
                signal_types["EXHAUST3_FADE"]=-current
            if sweep_long:
                signal_types["PRICE_SWEEP_REVERSE"]=1
            elif sweep_short:
                signal_types["PRICE_SWEEP_REVERSE"]=-1
            if prevsign==-current and volgreater:
                signal_types["COLOR_TURN_FOLLOW"]=current
                signal_types["COLOR_TURN_FADE"]=-current
            if not signal_types:continue
            for family,side in signal_types.items():
                event=(ts+STEP,inst,side,ratio,body,rank,daykey,
                       float(cnext[0]))
                events[family][key].append(event)
                count[family]+=1

        if (idx+1)%576==0:
            print(f"RANKING_EVENTS_PROGRESS={idx+1}/2304 FAMILIES={dict(count)}",flush=True)
    for byslot in events.values():
        for key,es in byslot.items():
            es.sort(key=lambda e:(e[0],e[1]))
    print(f"TOP200_COVERAGE={min(coverage)}..{max(coverage)} EVENTS={dict(count)}",flush=True)
    return events,count

def choose_group(candidates,selector):
    if selector=="TOP_RANK":
        return min(candidates,key=lambda e:(e[5],e[1]))
    if selector=="HIGHEST_VOL_RATIO":
        return min(candidates,key=lambda e:(-e[3],e[5],e[1]))
    if selector=="HIGHEST_BODY":
        return min(candidates,key=lambda e:(-e[4],e[5],e[1]))
    return min(candidates,key=lambda e:(-e[5],e[1]))

OUTCOME_CACHE={}
DATA={}

def outcome(e,exitconf,day):
    ts,inst,sign,vr,body,rank,d,price=e
    if d!=day or price<=0:return None
    tp,sl,hold=exitconf
    key=(inst,ts,sign,tp,sl,hold)
    if key in OUTCOME_CACHE:
        return OUTCOME_CACHE[key]
    bars=DATA[inst]
    deadline=dt_last=(datetime.fromisoformat(day+"T00:00:00").replace(tzinfo=UK)+timedelta(days=1)).timestamp()*1000
    # user wants each 24h test distinct; at midnight flatten the open position
    for i in range(hold):
        opent=ts+i*STEP
        if opent>=deadline:
            return None
        b=bars.get(opent)
        if b is None:return None
        # Stop prioritized if TP and SL occur within same candle.
        if sign>0:
            lose=b[2]<=price*(1-sl)
            win=b[1]>=price*(1+tp)
        else:
            lose=b[1]>=price*(1+sl)
            win=b[2]<=price*(1-tp)
        if lose:
            ret=(-sl*100-COST,opent+STEP,"SL")
            OUTCOME_CACHE[key]=ret
            return ret
        if win:
            ret=(tp*100-COST,opent+STEP,"TP")
            OUTCOME_CACHE[key]=ret
            return ret
        if opent+STEP>=deadline or i==hold-1:
            value=sign*(b[3]/price-1)*100-COST
            ret=(value,opent+STEP,"END")
            OUTCOME_CACHE[key]=ret
            return ret
    return None

def eval_config(cfg,dates,need_detailed=False):
    family,vm,bm,side,selector,exitid=cfg
    perday=[]
    for day in dates:
        results=[]
        # One signal in each UK 8h window with a fixed causal tie breaker.
        for win in (0,1,2):
            x=EVENTS.get(family,{}).get((day,win),())
            first=None
            same=[]
            for e in x:
                if e[3]<vm or e[4]<bm:
                    continue
                if side!=0 and e[2]!=side:
                    continue
                if first is None:first=e[0]
                if e[0]!=first:break
                same.append(e)
            if same:
                best=choose_group(same,selector)
                y=outcome(best,EXITS[exitid],day)
                if y:
                    pct,exit_ts,reason=y
                    results.append((best,pct,reason))
        gross=sum(z[1] for z in results)
        obj={"day":day,"trades":len(results),"wins_net":sum(z[1]>0 for z in results),
             "net_usdt":round(gross*NOTIONAL/100,5),
             "net_pct_reference":round(gross,4)}
        if need_detailed:
            obj["positions"]=[{"time_uk":datetime.fromtimestamp(e[0]/1000,UK).strftime("%H:%M"),
                                "inst":e[1],"rank":e[5],"side":"LONG" if e[2]>0 else "SHORT",
                                "vol_ratio":round(e[3],2),"body_pct":round(e[4],2),
                                "net_pct":round(pct,3),"exit_reason":reason} for e,pct,reason in results]
        perday.append(obj)
    return perday

def calc_score(days):
    n=[x["net_usdt"] for x in days]
    three=sum(x["trades"]==3 for x in days)
    positive=sum(x>0 for x in n)
    goal=sum(x>=.6 for x in n)
    return {"days":len(days),"three_entry_days":three,
            "positive_days":positive,
            "days_at_least_3pct":goal,
            "min_daily_usdt":round(min(n),4),
            "avg_daily_usdt":round(sum(n)/len(n),4),
            "net_total_usdt":round(sum(n),4),
            "per_day":days}

def main():
    global DATA,EVENTS
    DATA=load_data()
    EVENTS,count=events_from_history(DATA)
    train_dates=DAYNAMES[:4]
    validation=DAYNAMES[4:6]
    holdout=DAYNAMES[6:]
    SCANNED=0
    contenders=[]
    positive_train=0
    for fam,vm,bm,side,sel,exi in itertools.product(
            FAMILIES,VOL_THRESHOLDS,BODY_THRESHOLDS,(0,1,-1),SELECTORS,range(len(EXITS))):
        SCANNED+=1
        conf=(fam,vm,bm,side,sel,exi)
        train=eval_config(conf,train_dates)
        if any(x["trades"]!=3 for x in train):continue
        if sum(x["net_usdt"] for x in train)<=0:continue
        positive_train+=1
        contenders.append((conf,calc_score(train)))
    contenders.sort(key=lambda v:(v[1]["positive_days"],v[1]["days_at_least_3pct"],
                                  v[1]["avg_daily_usdt"],v[1]["min_daily_usdt"]),reverse=True)
    print(f"SEARCH_PROGRESS=SCANNED_{SCANNED} TRAIN_POSITIVE={positive_train} TRAIN_CANDIDATES={len(contenders)}",flush=True)
    valid=[]
    for cfg,tr in contenders:
        v=calc_score(eval_config(cfg,validation))
        if v["three_entry_days"]==2 and v["net_total_usdt"]>0:
            valid.append((cfg,tr,v))
    valid.sort(key=lambda t:(t[2]["positive_days"],t[1]["positive_days"]+t[2]["positive_days"],
                             t[2]["avg_daily_usdt"],t[1]["avg_daily_usdt"]),reverse=True)
    print(f"VALIDATION_POSITIVE={len(valid)}",flush=True)
    # Holdout should only be checked for best 20 preselected options, not
    # use it to pick the eventual recommendation (avoid leakage).
    preselected=valid[:20] if valid else [(cfg,tr,calc_score(eval_config(cfg,validation))) for cfg,tr in contenders[:20]]
    details=[]
    for cfg,tr,val in preselected[:8]:
        # no search/selection uses these last two dates
        test=calc_score(eval_config(cfg,holdout,need_detailed=True))
        tt=calc_score(eval_config(cfg,train_dates,need_detailed=True))
        vv=calc_score(eval_config(cfg,validation,need_detailed=True))
        fam,vm,bm,side,selector,eid=cfg
        details.append({
            "setup":{"family":fam,"vol_vs_median20_min":vm,"body_pct_min":bm,
                     "side":"BOTH" if side==0 else "LONG" if side>0 else "SHORT",
                     "select_simultaneous":selector,
                     "take_profit_pct":EXITS[eid][0]*100,
                     "stop_loss_pct":EXITS[eid][1]*100,
                     "max_hold_minutes":EXITS[eid][2]*5},
            "training":tt,"validation":vv,"holdout_untouched":test,
            "all_8_days_net_usdt":round(tt["net_total_usdt"]+vv["net_total_usdt"]+test["net_total_usdt"],4),
            "positive_each_day_of_8":tt["positive_days"]==4 and vv["positive_days"]==2 and test["positive_days"]==2,
            "three_completed_entries_each_day":tt["three_entry_days"]==4 and vv["three_entry_days"]==2 and test["three_entry_days"]==2})
    best_train=[{"family":c[0],"vmin":c[1],"bodymin":c[2],"side":c[3],
                 "selector":c[4],"exit":EXITS[c[5]],
                 "train":s,
                 "validation":calc_score(eval_config(c,validation))}
                for c,s in contenders[:5]]
    result={
      "dataset":"390 out of 390 currently live crypto USDT swaps; 8 full UK days 2026-10-09..16, all market OHLCV real from BloFin; current-listed universe (survivorship bias).",
      "ranking":"Recomputed TOP200 every closed 5m bar by 24h price % change using only past candles.",
      "execution":"One non-overlapping first valid entry from each 00-08/08-16/16-24 UK local window, contemporaneous signals chosen by fixed score known at entry. No 'best three in hindsight'.",
      "search":{"scenarios_tested":SCANNED,"positive_train_fourdays":positive_train,
                "positive_validation_bothdays":len(valid)},
      "costs":"0.12% roundtrip fees + 0.10% adverse slippage, gross exposure 20USDT/trade.",
      "best_8_preselected_without_looking_at_holdout":details,
      "best_train_only":best_train,
      "dataset_caveats":"Exclude delisted historic markets, ignores funding/liquidation; exact intrabar TP-SL path unknown so same-bar double-hit prioritized SL. No orders and no LIVE changes. Only 8 days and multi-search severe overfitting risk; 30-60d blind forward needed.",
    }
    Path("blofin_top200_new_volume_reversals_report.json").write_text(
         json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("NEW_VOLUME_REVERSALS_RESULT="+json.dumps(result,ensure_ascii=False,separators=(",",":")),flush=True)

if __name__=="__main__":
    main()
