"""READ ONLY: inspect entry causes in the 64 profitable versus 78 losing SHORT trades.
Original continuous Oct 17 18:00 to Oct 18 18:00 UK backtest. Only 3<=signal
volume / previous opposite GREEN candle volume <4. No orders or LIVE writes.
Provides causal-at-entry features separately from subsequent price behavior.
"""
from __future__ import annotations
from collections import Counter
import json
import math
import statistics
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import short60_volume_mean10_24hour_readonly as m

F=m.FIVE
UK=ZoneInfo("Europe/London")

def median(values):
    v=[float(x) for x in values if x is not None and math.isfinite(float(x))]
    return round(statistics.median(v),5) if v else None
def avg(values):
    v=[float(x) for x in values if x is not None and math.isfinite(float(x))]
    return round(statistics.fmean(v),5) if v else None
def rsi14(closes):
    # 14-change SIMPLE RSI (not Wilder RSI), only CLOSED historical candles.
    if len(closes)<15:return None
    d=[closes[i]-closes[i-1] for i in range(-14,0)]
    gains=sum(max(0,x) for x in d)
    losses=-sum(min(0,x) for x in d)
    if losses==0:return 100. if gains>0 else 50.
    return 100-100/(1+gains/losses)

def features(trade, bars):
    ts=trade["entry_ms"]-F
    b=bars.get(ts)
    if b is None:return None
    lookback=[bars.get(ts-i*F) for i in range(0,31)]
    if any(x is None for x in lookback):return None
    o,h,l,c,vol=b
    prior=lookback[1]
    open_buy=trade["entry_ms"]
    enter=bars.get(open_buy)
    close=bars.get(trade["exit_ms"]-F)
    if not enter or not close:return None
    prev_closes=[lookback[i][3] for i in range(14,-1,-1)]
    prev6=lookback[1:7]
    # Complete 5m signal bar knowledge at time of entry, no future leakage.
    upper=(h-max(o,c))/(h-l) if h>l else 0
    lower=(min(o,c)-l)/(h-l) if h>l else 0
    res={
        "coin":trade["inst"],"rank":trade["rank"],"entry_ms":trade["entry_ms"],
        "outcome":trade["outcome"],"profit_pct":trade["gross_pct"],
        "entry_volume_to_previous":trade["volume_ratio"],
        "entry_volume_to_mean10":trade["volume_mean10_ratio"],
        "signal_red_body_pct":100*(o-c)/o,
        "signal_body_fraction_of_range":abs(o-c)/(h-l) if h>l else 0,
        "signal_close_lower_range_fraction":(c-l)/(h-l) if h>l else 0,
        "upper_wick_fraction":upper,"lower_wick_fraction":lower,
        "previous_green_body_pct":100*(prior[3]-prior[0])/prior[0] if prior[0] else None,
        "prior6_red_count":sum(lookback[j][3]<lookback[j][0] for j in range(1,7)),
        "prior6_green_count":sum(lookback[j][3]>lookback[j][0] for j in range(1,7)),
        "price_change_prior3_pct":100*(c/lookback[3][3]-1),
        "price_change_prior6_pct":100*(c/lookback[6][3]-1),
        "price_change_prior12_pct":100*(c/lookback[12][3]-1),
        "price_change_prior30_pct":100*(c/lookback[30][3]-1),
        "rsi14_simple":rsi14(prev_closes),
        "sma10_minus_sma20_pct":100*((sum(lookback[j][3] for j in range(10))/10)
                                     /(sum(lookback[j][3] for j in range(20))/20)-1),
        "sma10_slope_pct":100*((sum(lookback[j][3] for j in range(10))/10)
                               /(sum(lookback[j][3] for j in range(1,11))/10)-1),
        "hour_uk":datetime.fromtimestamp(trade["entry_ms"]/1000,UK).hour,
        # Post-entry values diagnose outcome; NOT valid for entry decisions.
        "holding_min":(trade["exit_ms"]-trade["entry_ms"])/60000,
        "change_price_next5_pct":100*(bars[open_buy][3]/enter[0]-1) if open_buy in bars else None,
        "exit_price_change_pct":100*(close[3]/enter[0]-1),
    }
    return res

def by_flag(sample,feature,pred):
    result=[]
    for state in (True,False):
        ss=[x for x in sample if x.get(feature) is not None and bool(pred(x[feature]))==state]
        w=sum(x["outcome"]=="WIN" for x in ss)
        l=sum(x["outcome"]=="LOSS" for x in ss)
        result.append({"condition_met":state,"wins":w,"losses":l,"success_pct":round(100*w/(w+l),2) if w+l else None})
    return result

def main():
    data,failed,total=m.load()
    assert total==390 and len(data)==390 and not failed
    rank,coverage=m.rankings(data)
    baseline=m.test_period(data,rank,m.START,m.END,1.6)
    assert (baseline["wins"],baseline["losses"])==(528,649)
    chosen=[x for x in baseline["trades"] if
         x["outcome"] in ("WIN","LOSS") and
         x["volume_ratio"] is not None and 3<=x["volume_ratio"]<4]
    feats=[features(t,data[t["inst"]]) for t in chosen]
    if any(f is None for f in feats):raise RuntimeError("Missing feature OHLCVs")
    c=Counter(t["outcome"] for t in feats)
    print("REPRODUCED_GROUP="+json.dumps(c),flush=True)
    if (c["WIN"],c["LOSS"])!=(64,78):raise RuntimeError("Not 64 wins and 78 losses; cannot attribute results")
    numeric=[
     "entry_volume_to_previous","entry_volume_to_mean10","signal_red_body_pct",
     "signal_body_fraction_of_range","signal_close_lower_range_fraction",
     "upper_wick_fraction","lower_wick_fraction",
     "previous_green_body_pct","prior6_red_count","price_change_prior3_pct",
     "price_change_prior6_pct","price_change_prior12_pct","price_change_prior30_pct",
     "rsi14_simple","sma10_minus_sma20_pct","sma10_slope_pct",
     "holding_min","change_price_next5_pct"
    ]
    stats={}
    for label in ("WIN","LOSS"):
        t=[f for f in feats if f["outcome"]==label]
        stats[label]={"n":len(t),"median_rank":median([x["rank"] for x in t]),
                      "median_features":{k:median([x[k] for x in t]) for k in numeric},
                      "mean_features":{k:avg([x[k] for x in t]) for k in numeric}}
    thresholds=[
        ("price_change_prior3_pct","prior3_falling",lambda x:x<0),
        ("price_change_prior6_pct","prior6_falling",lambda x:x<0),
        ("price_change_prior12_pct","prior12_falling",lambda x:x<0),
        ("rsi14_simple","rsi_below50",lambda x:x<50),
        ("sma10_minus_sma20_pct","sma10_below_sma20",lambda x:x<0),
        ("signal_red_body_pct","signal_body_gt0point2pct",lambda x:x>=.2),
        ("entry_volume_to_mean10","vol_50pct_to_mean10",lambda x:x>=.5),
        ("signal_close_lower_range_fraction","signal_close_bottom25pct",lambda x:x<=.25),
        ("prior6_red_count","prior6_majority_red",lambda x:x>=4),
        ("price_change_prior30_pct","prior30_falling",lambda x:x<0),
    ]
    entry_comparisons={}
    for key,label,pred in thresholds:
        entry_comparisons[label]=by_flag(feats,key,pred)
    afterentry={}
    for k,label,fn in [
        ("change_price_next5_pct","next_5m_close_lower",lambda v:v<0),
        ("holding_min","held_15m_or_less",lambda v:v<=15)
    ]: afterentry[label]=by_flag(feats,k,fn)
    report={
      "source":"Oct 17 18:00 to Oct 18 18:00 UK; 390 BloFin markets, 24h historical TOP200",
      "subset":"Only 3.0 <= SHORT signal candle volume / preceding green candle volume <4.0, signal volume <= previous 10 5m candles' arithmetic mean, exit on valid opposite-volume reversal. Only completed wins/losses.",
      "group_count":dict(c),
      "by_outcome":stats,
      "entry_filters":entry_comparisons,
      "after_entry_diagnostic_only":afterentry,
      "trade_features":feats,
      "caveats":"No single feature proves cause or future trade success; outcome already known in retrospective cohort, multiple comparisons; time and postentry stats must not be used as entry filters. Trades exclude fees, slippage, spread, funding, and can overlap across coins. No LIVE files touched."}
    p=Path("short60_subset_3to4x_winloss_explanation.json")
    p.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("SHORT_64_WINNER_FACTORS="+json.dumps({k:v for k,v in report.items() if k!="trade_features"},ensure_ascii=False,separators=(",",":")),flush=True)
if __name__=="__main__":main()
