"""READ ONLY causal forensic compare: 295 trade wins vs losses on 390 BloFin coin TOP200.

Uses unchanged public BloFin OHLCV candle shards from completed 8d research.
Captures exact original 295 serialized executed trades. Does not alter LIVE,
send orders, introduce estimated fills, or refit outcomes. Filtering is a
SECOND exploratory experiment and is evaluated on independent later days.
"""
import collections
import datetime as dt
import itertools
import json
import math
import statistics
from pathlib import Path

import blofin_top200_repeat_setup_research as base
import blofin_top200_parallel_runner as source

original_calc=base.calc_event
original_compute=base.compute_daily
MARKETS={}
STATES={}
EVENTS={}
OUTCOMES={}


def capture_event(inst,ts,data,states,rank):
    if inst not in MARKETS:
        MARKETS[inst]=data
        STATES[inst]=states
    return original_calc(inst,ts,data,states,rank)

def capture_computed(candidates,outcomes,rule,exitid,days):
    EVENTS.update(candidates)
    OUTCOMES.update(outcomes)
    return original_compute(candidates,outcomes,rule,exitid,days)

base.calc_event=capture_event
base.compute_daily=capture_computed
source.analyze()
base.calc_event=original_calc
base.compute_daily=original_compute

family="PULLBACK"
threshold_ratio=1.7
threshold_body=.4
exitid=base.EXIT_LIST.index((.02,.01,12))
days=[(source.anchor().replace(hour=0)-base.timedelta(days=base.NUM_DAYS-i)).date().isoformat() for i in range(base.NUM_DAYS)]

rows=sorted([e for e in EVENTS[family] if e["ratio"]>=threshold_ratio
             and e["body"]>=threshold_body],key=lambda e:(e["signal_ms"],e["inst"]))
selected=[]


def median(xs):
    nums=[x for x in xs if x is not None and math.isfinite(x)]
    return statistics.median(nums) if nums else None

def avg(xs):
    nums=[x for x in xs if x is not None and math.isfinite(x)]
    return statistics.mean(nums) if nums else None

def rounded(x,n=3):
    return None if x is None else round(x,n)

def indicators(e):
    inst=e["inst"]
    data=MARKETS[inst]
    states=STATES[inst]
    t=e["sig_ts"]
    b=data.get(t)
    if b is None:return {}
    o,h,l,c,v=b
    sign=1 if e["side"]=="LONG" else -1
    def hist(i):
        return data.get(t-i*base.FIVE)
    past=[hist(i) for i in range(55,-1,-1)]
    if any(z is None for z in past):
        return {}
    prev=hist(1)
    curr_index=len(past)-1
    closes=[x[3] for x in past]
    # Wilder 14-period RSI. Up/down change using last 50 intervals
    diffs=[closes[j]-closes[j-1] for j in range(1,len(closes))]
    gains=[max(0,d) for d in diffs]
    losses=[max(0,-d) for d in diffs]
    gp=sum(gains[:14])/14
    ls=sum(losses[:14])/14
    for ix in range(14,len(gains)):
        gp=(gp*13+gains[ix])/14
        ls=(ls*13+losses[ix])/14
    rsi=(100-(100/(1+gp/ls))) if ls>0 else (100 if gp>0 else 50)
    longbias= rsi if sign==1 else (100-rsi)
    kbars=past[-14:]
    lo14=min(x[2] for x in kbars)
    hi14=max(x[1] for x in kbars)
    stoch=100*(c-lo14)/(hi14-lo14) if hi14>lo14 else 50
    stochdir=stoch if sign==1 else 100-stoch

    # ADX14 based on 55 lookback, Wilder smoothing.
    tr=[]; plus=[];minus=[]
    for i in range(1,len(past)):
        q, p=past[i],past[i-1]
        upward=q[1]-p[1]
        downward=p[2]-q[2]
        plus.append(upward if upward>downward and upward>0 else 0)
        minus.append(downward if downward>upward and downward>0 else 0)
        tr.append(max(q[1]-q[2],abs(q[1]-p[3]),abs(q[2]-p[3])))
    atr14=sum(tr[:14])
    plus14=sum(plus[:14])
    minus14=sum(minus[:14])
    dx=[]
    for i in range(14,len(tr)):
        atr14=atr14-atr14/14+tr[i]
        plus14=plus14-plus14/14+plus[i]
        minus14=minus14-minus14/14+minus[i]
        pdi=100*plus14/atr14 if atr14 else 0
        mdi=100*minus14/atr14 if atr14 else 0
        dx.append(100*abs(pdi-mdi)/(pdi+mdi) if pdi+mdi>0 else 0)
    adx=avg(dx[-14:]) if dx else None
    atrpct=100*atr14/14/c

    ma10=sum(closes[-10:])/10
    ma20=sum(closes[-20:])/20
    prevma10=sum(closes[-15:-5])/10
    recent6=past[-7:-1]
    relative_peak= (c-max(x[1] for x in recent6)) if sign==1 else (min(x[2] for x in recent6)-c)
    clock=dt.datetime.fromtimestamp(e["signal_ms"]/1000,base.UK)
    body_abs=100*abs(c/o-1)
    close_relative= (c-l)/(h-l) if h>l else .5
    if sign<0:close_relative=1-close_relative
    last5=hist(5)
    last20=hist(20)
    past_vols=[hist(i)[4] for i in range(1,21)]
    pos=states.get(t,(None,False,0))
    volratio=e["ratio"]
    return {
        "volume_vs_20median":volratio,
        "volume_vs_previous":v/prev[4] if prev[4] else None,
        "body_pct":body_abs,
        "rank_24h":e["rank"],
        "rsi14":rsi,
        "rsi_directional":longbias,
        "stoch14_directional":stochdir,
        "adx14":adx,
        "atr14_pct":atrpct,
        "ma10_signed_gap_pct":sign*100*(c/ma10-1) if ma10 else None,
        "ma20_signed_gap_pct":sign*100*(c/ma20-1) if ma20 else None,
        "ma10_signed_slope_25min_pct":sign*100*(ma10/prevma10-1) if prevma10 else None,
        "momentum_25min_pct":sign*100*(c/last5[3]-1) if last5[3] else None,
        "momentum_100min_pct":sign*100*(c/last20[3]-1) if last20[3] else None,
        "close_on_directional_wick_0to1":close_relative,
        "breakout6_directional_pct":100*relative_peak/c,
        "prev_body_pct":100*abs(prev[3]/prev[0]-1) if prev[0] else None,
        "local_uk_hour":clock.hour,
        "sign":sign
    }


FEATURE_CACHE={}
def feats(e):
    k=(e["inst"],e["signal_ms"],e["side"])
    if k not in FEATURE_CACHE:
        FEATURE_CACHE[k]=indicators(e)
    return FEATURE_CACHE[k]


def serial(events,limit=1000000,pred=lambda e,f:True):
    chosen=[]
    busy=0
    counter=collections.Counter()
    for e in events:
        if counter[e["day"]]>=limit or e["signal_ms"]<busy:
            continue
        outcome=OUTCOMES.get((e["inst"],e["signal_ms"],e["side"],exitid))
        if outcome is None:continue
        f=feats(e)
        if not f: continue
        if not pred(e,f):continue
        pct,end,reason=outcome
        chosen.append({**e,**f,"gross_pct":pct,
                       "net_pct":pct-base.COST_PCT,
                       "net_usdt":base.NOTIONAL*(pct-base.COST_PCT)/100,
                       "exit_ms":end,"exit_reason":reason,
                       "gross_win":pct>0,
                       "net_win":pct>base.COST_PCT})
        busy=end
        counter[e["day"]]+=1
    return chosen

trade=serial(rows)
expected=295
if len(trade)!=expected:
    raise RuntimeError(f"Ledger differs from original 295 trades: got {len(trade)}")
if sum(t["gross_win"] for t in trade)!=128:
    raise RuntimeError("Unexpected gross winner count")
print("REPRODUCED="+json.dumps({
    "trades":len(trade),"gross_wins":sum(t["gross_win"] for t in trade),
    "net_wins":sum(t["net_win"] for t in trade),
    "net_usdt":round(sum(t["net_usdt"] for t in trade),4)
}),flush=True)

def summarize(tradegroup):
    pnl=sum(t["net_usdt"] for t in tradegroup)
    return {"trades":len(tradegroup),"win_net_count":sum(t["net_win"] for t in tradegroup),
            "win_net_percent":round(100*sum(t["net_win"] for t in tradegroup)/len(tradegroup),2) if tradegroup else None,
            "net_usdt":round(pnl,4),
            "avg_net_pct_per_trade":round(100*pnl/(len(tradegroup)*base.NOTIONAL),3) if tradegroup else None,
            "avg_usdt_per_trade":round(pnl/len(tradegroup),4) if tradegroup else None}
winning=[t for t in trade if t["net_win"]]
losing=[t for t in trade if not t["net_win"]]
groups={}
for name,pred in {
    "LONG": lambda x:x["side"]=="LONG",
    "SHORT":lambda x:x["side"]=="SHORT",
    "UK_00_08":lambda x:x["local_uk_hour"]<8,
    "UK_08_16":lambda x:8<=x["local_uk_hour"]<16,
    "UK_16_24":lambda x:x["local_uk_hour"]>=16,
    "RANK_1_50":lambda x:x["rank_24h"]<=50,
    "RANK_51_100":lambda x:50<x["rank_24h"]<=100,
    "RANK_101_200":lambda x:x["rank_24h"]>100,
}.items():
    g=[t for t in trade if pred(t)]
    groups[name]=summarize(g)

METRICS=("volume_vs_20median","volume_vs_previous","body_pct","rank_24h","rsi14",
         "rsi_directional","stoch14_directional","adx14","atr14_pct",
         "ma10_signed_gap_pct","ma20_signed_gap_pct","ma10_signed_slope_25min_pct",
         "momentum_25min_pct","momentum_100min_pct",
         "close_on_directional_wick_0to1","breakout6_directional_pct",
         "prev_body_pct","local_uk_hour")
winlose={}
for m in METRICS:
    winlose[m]={"winning_median":rounded(median(t.get(m) for t in winning)),
                "losing_median":rounded(median(t.get(m) for t in losing)),
                "winning_mean":rounded(avg(t.get(m) for t in winning)),
                "losing_mean":rounded(avg(t.get(m) for t in losing))}
exit_reasons={}
for reason in sorted(set(t["exit_reason"] for t in trade)):
    exit_reasons[reason]=summarize([t for t in trade if t["exit_reason"]==reason])

FILTERS={
  "BASELINE_ALL":lambda e,f:True,
  "ONLY_LONG":lambda e,f:e["side"]=="LONG",
  "ONLY_SHORT":lambda e,f:e["side"]=="SHORT",
  "RANK_1_50":lambda e,f:f["rank_24h"]<=50,
  "RANK_51_100":lambda e,f:51<=f["rank_24h"]<=100,
  "RANK_101_200":lambda e,f:f["rank_24h"]>=101,
  "VOLUME_RATIO_2_5":lambda e,f:f["volume_vs_20median"]>=2.5,
  "VOLUME_RATIO_4":lambda e,f:f["volume_vs_20median"]>=4,
  "VOLUME_RATIO_LE3":lambda e,f:f["volume_vs_20median"]<=3,
  "VOLUME_PREV_GE2":lambda e,f:f["volume_vs_previous"]>=2,
  "BODY_GE0_7":lambda e,f:f["body_pct"]>=.7,
  "BODY_GE1":lambda e,f:f["body_pct"]>=1,
  "BODY_LE0_7":lambda e,f:f["body_pct"]<=.7,
  "SIGNAL_RSI_DIRECTIONAL_GE50":lambda e,f:f["rsi_directional"]>=50,
  "SIGNAL_RSI_DIRECTIONAL_40_65":lambda e,f:40<=f["rsi_directional"]<=65,
  "SIGNAL_RSI_DIRECTIONAL_LE40":lambda e,f:f["rsi_directional"]<=40,
  "STOCH_DIR_GE60":lambda e,f:f["stoch14_directional"]>=60,
  "STOCH_DIR_LE40":lambda e,f:f["stoch14_directional"]<=40,
  "ADX_LE25":lambda e,f:f["adx14"] is not None and f["adx14"]<=25,
  "ADX_25_TO_38":lambda e,f:f["adx14"] is not None and 25<=f["adx14"]<=38,
  "ADX_GE38":lambda e,f:f["adx14"] is not None and f["adx14"]>=38,
  "ATR_GE1":lambda e,f:f["atr14_pct"]>=1,
  "ATR_LE1":lambda e,f:f["atr14_pct"]<=1,
  "ABOVE_DIRECTIONAL_MA10":lambda e,f:f["ma10_signed_gap_pct"]>0,
  "MA10_TREND_CONFIRMED":lambda e,f:f["ma10_signed_slope_25min_pct"]>0,
  "MA20_TREND_CONFIRMED":lambda e,f:f["ma20_signed_gap_pct"]>0,
  "MOMENTUM_25MIN_GE0":lambda e,f:f["momentum_25min_pct"]>0,
  "MOMENTUM_100MIN_GE0":lambda e,f:f["momentum_100min_pct"]>0,
  "WICK_CLOSE_STRONG_GE0_75":lambda e,f:f["close_on_directional_wick_0to1"]>=.75,
  "BREAKS_6_PREVIOUS_EXTREME":lambda e,f:f["breakout6_directional_pct"]>0,
  "HOURS_UK_00_08":lambda e,f:f["local_uk_hour"]<8,
  "HOURS_UK_08_16":lambda e,f:8<=f["local_uk_hour"]<16,
  "HOURS_UK_16_24":lambda e,f:f["local_uk_hour"]>=16,
}
print("START_FILTERS_COUNT="+str(len(FILTERS)),flush=True)
train_days=set(days[:4])
validate_days=set(days[4:6])
holdout_days=set(days[6:8])

def seg(rows,days_to_include):
    selected=[x for x in rows if x["day"] in days_to_include]
    result=summarize(selected)
    result["days_with_at_least_3_entries"]=sum(sum(x["day"]==day for x in selected)>=3 for day in days_to_include)
    result["days_profitable"]=sum(sum(x["net_usdt"] for x in selected if x["day"]==day)>0 for day in days_to_include)
    result["daily_net_usdt"]={day:round(sum(x["net_usdt"] for x in selected if x["day"]==day),4) for day in sorted(days_to_include)}
    return result

filter_results=[]
for name,fn in FILTERS.items():
    entries=serial(rows,pred=fn)
    tr=seg(entries,train_days)
    va=seg(entries,validate_days)
    ho=seg(entries,holdout_days)
    filter_results.append({"filter":name,"train":tr,"validation":va,"holdout":ho,
        "total":summarize(entries)})
basev=next(x for x in filter_results if x["filter"]=="BASELINE_ALL")
# Independent check for 2-filter conjunctions derived only from TRAIN
single_best=sorted((x for x in filter_results if x["filter"]!="BASELINE_ALL"
                 and x["train"]["trades"]>=20
                 and x["train"]["days_with_at_least_3_entries"]>=3),
                 key=lambda x:(x["train"]["net_usdt"],
                               x["train"]["win_net_percent"]),reverse=True)
print("FILTERS_FINISHED="+str(len(filter_results)),flush=True)
top_combination=[]
top_names=[x["filter"] for x in single_best[:7]]
for a,b in itertools.combinations(top_names,2):
    merged=serial(rows,pred=lambda e,f,a=a,b=b:FILTERS[a](e,f) and FILTERS[b](e,f))
    tr=seg(merged,train_days)
    if tr["trades"]<12 or tr["days_with_at_least_3_entries"]<3:continue
    top_combination.append({"filters":[a,b],"train":tr,
                            "validation":seg(merged,validate_days),
                            "holdout":seg(merged,holdout_days),
                            "total":summarize(merged)})
top_combination.sort(key=lambda x:x["train"]["net_usdt"],reverse=True)
report={
  "dates_uk":days,
  "exchange":"BloFin 390 present USDT swaps, top200 per 5m rolling 24h price change (not historical delisted instrument universe).",
  "baseline_295":{"total":summarize(trade),
                  "train":seg(trade,train_days),
                  "validation":seg(trade,validate_days),
                  "holdout":seg(trade,holdout_days)},
  "winners_net":len(winning),
  "losers_or_flat_net":len(losing),
  "gross_winners":sum(t["gross_win"] for t in trade),
  "win_vs_loss_indicators":winlose,
  "categories":groups,
  "exit_reasons":exit_reasons,
  "filters_sorted_by_training_only":sorted(filter_results,key=lambda x:x["train"]["net_usdt"],reverse=True),
  "top_train_two_filter_combos":top_combination[:8],
  "limitations":"Descriptive comparisons on 295 chronological simulated trades; filter backtests reevaluate entry events and exit times. 4d train, 2d validation, 2d untouched holdout. Feature values all from signal closed 5m candle; 0.22% fee+slippage roundtrip, funding excluded. Simulated OHLC TP/SL, intrabar ambiguous both-hit conservative stop. Searching multiple filters can overfit; do not claim verified without positive forward holdout. No actual trade executions or LIVE modifications."
}
Path("blofin_top200_wins_vs_losses_analysis.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("WIN_LOSS_DIFFERENCES="+json.dumps({
    "baseline":report["baseline_295"],
    "categories":groups,
    "exit_reasons":exit_reasons,
    "indicators":winlose,
    "best_train_filters":report["filters_sorted_by_training_only"][:9],
    "two_filter_combos":top_combination[:3]
},ensure_ascii=False,separators=(",",":")),flush=True)
