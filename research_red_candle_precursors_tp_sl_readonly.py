"""READ ONLY: forensic candle sequences BEFORE the profitable red-body/SMA10 LONG entry.

User-selected setup: TOP200 BloFin dynamically selected by prior-24h close
growth among 388 complete-history of 390 currently traded crypto USDT swaps.
Closed signal is RED 5m body from 0.40 to <0.80%, and signal close <= SMA10
by at least 2.00%; LONG on next 5m open; first TP+1.5% or SL-1.0%,
or 60m timeout. Estimate costs of 0.22% roundtrip. SL first if both
price levels crossed within same candle.

In this research all "preceding features" are taken EXCLUSIVELY from
candles before the signal 5m candle; never from signal or future bar.
Group TP/SL/TIME, relative win likelihood, and sequential no-overlap.
Train first four complete days, validate next three, test last two and part
of third without using that part in ranking any rule.
No LIVE strategy files touched, no API creds or orders.
"""
import collections
import gzip
import json
import statistics
import math
from datetime import datetime
from pathlib import Path

import research_red_body04_oversold_long_9days_readonly as base

STEP=base.FIVE
START=base.START
TRAINEND=base.TRAINEND
VALIDEND=base.VALIDEND
END=base.END
COST=base.COST
NOTIONAL=base.NOTIONAL

def color(b):
    if b[3]>b[0]:return "G"
    if b[3]<b[0]:return "R"
    return "D"

def all_previous_features(bars,t,rsis):
    """t is the signal candle OPEN, prior bars have k <= t-STEP."""
    b=[bars[t-i*STEP] for i in range(1,22)]
    p=b[0];prior=b[1]
    c1=color(p)
    prev3=b[:3]
    prev5=b[:5]
    prev6=b[:6]
    r=lambda x:(x[3]/x[0]-1)*100
    ranges=lambda x:x[1]-x[2]
    body=lambda x:abs(r(x))
    pc=r(p)
    colors="".join(color(v) for v in b[:5])
    datestring=base.when(t)
    median=statistics.median(x[4] for x in b[:20])
    previous_price=p[3]
    closes=[z[3] for z in b[:20]]
    ma10=sum(closes[:10])/10
    ma20=sum(closes)/20
    dist10=(previous_price/ma10-1)*100
    dist20=(previous_price/ma20-1)*100
    p3_price=(p[3]/b[3][3]-1)*100
    p6_price=(p[3]/b[6][3]-1)*100
    p12_price=(p[3]/b[12][3]-1)*100
    before_rsi=rsis.get(t-STEP)
    rsi_prior3=rsis.get(t-4*STEP)
    low3=min(z[2] for z in prev3)
    low6=min(z[2] for z in prev6)
    low_prior6=min(z[2] for z in b[1:7])
    move_from_prior6_high=(previous_price/max(z[1] for z in b[1:7])-1)*100
    red3=sum(color(z)=="R" for z in prev3)
    red5=sum(color(z)=="R" for z in prev5)
    red6=sum(color(z)=="R" for z in prev6)
    last3_red_vol=sum(z[4] for z in prev3 if color(z)=="R")
    last3_green_vol=sum(z[4] for z in prev3 if color(z)=="G")
    run=0
    for z in b[:6]:
        if color(z)=="R":run+=1
        else:break
    rb=[r(z) for z in b[:6]]
    prior_v2=(p[4]/prior[4] if prior[4]>0 else None)
    signal_hour=datetime.fromtimestamp(t/1000,base.UK).hour
    feats=[]
    def add(k,c):
        if c:feats.append(k)
    add("P1_RED",c1=="R")
    add("P1_GREEN",c1=="G")
    add("P1_DOJI",c1=="D")
    add("P1_RED_BODY_GE0P2",pc<=-.2)
    add("P1_RED_BODY_GE0P4",pc<=-.4)
    add("P1_RED_BODY_GE0P8",pc<=-.8)
    add("P1_RED_BODY_0P2_0P6",-.6<=pc<=-.2)
    add("P1_GREEN_BODY_GE0P2",pc>=.2)
    add("P1_GREEN_BODY_GE0P4",pc>=.4)
    add("P1_SMALL_BODY_LT0P1",abs(pc)<.1)
    add("P1_SMALL_BODY_LT0P2",abs(pc)<.2)
    add("P1_RED_LOWER_WICK_GE30PCT",
        c1=="R" and ranges(p)>0 and (p[3]-p[2])/ranges(p)>=.3)
    add("P1_RED_UPPER_WICK_GE30PCT",
        c1=="R" and ranges(p)>0 and (p[1]-p[0])/ranges(p)>=.3)
    add("P1_GREEN_LOWER_WICK_GE30PCT",
        c1=="G" and ranges(p)>0 and (p[0]-p[2])/ranges(p)>=.3)
    add("P1_RED_WITH_LONG_BODY_80PCT",
        c1=="R" and ranges(p)>0 and (p[0]-p[3])/ranges(p)>=.8)
    add("P1_LOWER_WICK_OVER_BODY",
        ranges(p)>0 and (min(p[0],p[3])-p[2])>=abs(p[0]-p[3]))
    add("P1_CLOSE_BOTTOM_20PCT",
        ranges(p)>0 and (p[3]-p[2])/ranges(p)<=.2)
    add("P1_CLOSE_TOP_20PCT",
        ranges(p)>0 and (p[3]-p[2])/ranges(p)>=.8)
    add("P1_VOLUME_GE_MEDIAN_2X",median>0 and p[4]>=2*median)
    add("P1_VOLUME_GE_MEDIAN_4X",median>0 and p[4]>=4*median)
    add("P1_VOLUME_GE_MEDIAN_10X",median>0 and p[4]>=10*median)
    add("P1_VOLUME_LT_MEDIAN",median>0 and p[4]<median)
    add("P1_VOLUME_RISING",p[4]>prior[4])
    add("P1_VOLUME_FALLING",p[4]<prior[4])
    add("P1_VOLUME_SPIKE_GE2X_PRIOR",prior_v2 is not None and prior_v2>=2)
    add("P1_VOLUME_DROP_LE_HALF_PRIOR",prior_v2 is not None and prior_v2<=.5)
    add("P1_VOLUME_LOWEST_IN_LAST3",p[4]<=min(z[4] for z in prev3))
    add("P1_VOLUME_HIGHEST_IN_LAST3",p[4]>=max(z[4] for z in prev3))
    add("P1_LOW_BROKEN_PRIOR6",p[2]<low_prior6)
    add("P1_CLOSE_BELOW_PRIOR6_LOW",p[3]<low_prior6)
    add("P1_NOT_NEW_LOW_PRIOR6",p[2]>=low_prior6)
    add("P1_CLOSE_LT_MA10_MINUS0P5",dist10<=-.5)
    add("P1_CLOSE_LT_MA10_MINUS1",dist10<=-1)
    add("P1_CLOSE_LT_MA10_MINUS2",dist10<=-2)
    add("P1_CLOSE_LT_MA20_MINUS1",dist20<=-1)
    add("P1_CLOSE_LT_MA20_MINUS2",dist20<=-2)
    add("P1_MA10_BELOW_MA20",ma10<ma20)
    add("P1_MA10_ABOVE_MA20",ma10>ma20)
    add("P1_RSI_LT25",before_rsi is not None and before_rsi<25)
    add("P1_RSI_LT30",before_rsi is not None and before_rsi<30)
    add("P1_RSI_LT35",before_rsi is not None and before_rsi<35)
    add("P1_RSI_LT40",before_rsi is not None and before_rsi<40)
    add("P1_RSI_LT45",before_rsi is not None and before_rsi<45)
    add("P1_RSI_25_40",before_rsi is not None and 25<=before_rsi<40)
    add("P1_RSI_30_45",before_rsi is not None and 30<=before_rsi<45)
    add("P1_RSI_35_50",before_rsi is not None and 35<=before_rsi<50)
    add("P1_RSI_40_55",before_rsi is not None and 40<=before_rsi<55)
    add("P1_RSI_RISING_3",before_rsi is not None and rsi_prior3 is not None and before_rsi>rsi_prior3)
    add("P1_RSI_FALLING_3",before_rsi is not None and rsi_prior3 is not None and before_rsi<rsi_prior3)
    add("P2_RED",color(b[1])=="R")
    add("P3_RED",color(b[2])=="R")
    add("P4_RED",color(b[3])=="R")
    add("PREV2_BOTH_RED",colors[:2]=="RR")
    add("PREV3_ALL_RED",colors[:3]=="RRR")
    add("PREV4_ALL_RED",colors[:4]=="RRRR")
    add("PREV5_ALL_RED",colors[:5]=="RRRRR")
    add("PREV2_BOTH_GREEN",colors[:2]=="GG")
    add("PREV3_ALL_GREEN",colors[:3]=="GGG")
    add("PREV3_2PLUS_RED",red3>=2)
    add("PREV3_ONLY_ONE_RED",red3==1)
    add("PREV3_NO_RED",red3==0)
    add("PREV5_4PLUS_RED",red5>=4)
    add("PREV6_5PLUS_RED",red6>=5)
    add("PREV3_RED_VOL_GT_GREEN",last3_red_vol>last3_green_vol)
    add("PREV3_VOL_RISING",b[2][4]<b[1][4]<b[0][4])
    add("PREV3_VOL_FALLING",b[2][4]>b[1][4]>b[0][4])
    add("PREV3_VOL_LAST_GT_BOTH",b[0][4]>max(b[1][4],b[2][4]))
    add("PREV3_VOL_LAST_LT_BOTH",b[0][4]<min(b[1][4],b[2][4]))
    add("PREV3_LAST_PRICE_DROP_0P5",p3_price<=-.5)
    add("PREV3_LAST_PRICE_DROP_1",p3_price<=-1)
    add("PREV6_LAST_PRICE_DROP_1",p6_price<=-1)
    add("PREV6_LAST_PRICE_DROP_2",p6_price<=-2)
    add("PREV6_LAST_PRICE_DROP_3",p6_price<=-3)
    add("PREV12_LAST_PRICE_DROP_2",p12_price<=-2)
    add("PREV12_LAST_PRICE_DROP_3",p12_price<=-3)
    add("PREV12_LAST_PRICE_DROP_4",p12_price<=-4)
    add("PREV6_LAST_PRICE_GAIN",p6_price>0)
    add("PREV3_RED_SIZE_ACCELERATING",rb[0]<rb[1]<rb[2]<0)
    add("PREV3_RED_SIZE_DECELERATING",rb[2]<rb[1]<rb[0]<0)
    add("PREV6_MAKE_LOWER_LOWS",all(prev6[i][2]<prev6[i+1][2] for i in range(5)))
    add("PREV6_MAKE_LOWER_CLOSES",all(prev6[i][3]<prev6[i+1][3] for i in range(5)))
    add("P1_REBOUNDED_FROM_PRIOR6_LOW_1PCT",low_prior6>0 and (p[3]/low_prior6-1)*100>1)
    add("PREV3_CLOSINGS_FALL",b[0][3]<b[1][3]<b[2][3])
    add("PREV3_CLOSINGS_RISE",b[0][3]>b[1][3]>b[2][3])
    add("PREV6_TOTAL_BODY_RED_BIGGER",sum((z[0]-z[3]) for z in b[:6])>0)
    add("PREV3_RED_CANDLE_WITH_LOWER_WICK",
        any(color(z)=="R" and ranges(z)>0 and (z[3]-z[2])/ranges(z)>=.3 for z in prev3))
    add("PREV3_VOL_LT_PREV3_PRIOR",
        sum(z[4] for z in b[:3])<sum(z[4] for z in b[3:6]))
    add("PREV3_VOL_GT_PREV3_PRIOR",
        sum(z[4] for z in b[:3])>sum(z[4] for z in b[3:6]))
    return {"tags":feats,"recent_3_colors":colors[:3],"recent_4_colors":colors[:4],
            "recent_5_colors":colors[:5],"rsi_prev":round(before_rsi,2) if before_rsi is not None else None,
            "prev_candle_body_pct":round(pc,4),
            "prev_candle_vol_median_mult":round(p[4]/median,3) if median else None,
            "prev_3_return_pct":round(p3_price,4),
            "prev_6_return_pct":round(p6_price,4),
            "prev_12_return_pct":round(p12_price,4),
            "prev_ma10_distance_pct":round(dist10,4),
            "red_previous_3_count":red3,
            "red_previous_6_count":red6}

def summary(rows):
    n=len(rows)
    if n==0:return {"n":0,"tp":0,"sl":0,"time":0,"net_usdt":0.0}
    return {"n":n,"tp":sum(e["reason"]=="TP" for e in rows),
      "sl":sum(e["reason"]=="SL" for e in rows),
      "time":sum(e["reason"]=="TIME" for e in rows),
      "tp_ratio_pct":round(100*sum(e["reason"]=="TP" for e in rows)/n,1),
      "net_positive":sum(e["ret"]>0 for e in rows),
      "net_pct_avg":round(sum(e["ret"] for e in rows)/n,4),
      "net_usdt":round(sum(e["ret"] for e in rows)*.2,4)}

def serial(rows,lo,hi):
    out=[];busy=lo
    for e in sorted(rows,key=lambda z:(z["entry"],z["rank"],z["inst"])):
        if lo<=e["entry"] and e["exit"]<=hi and e["entry"]>=busy:
            out.append(e);busy=e["exit"]
    return out

def main():
    raw,excluded=base.load()
    rsi={inst:base.rsi_wilder(bars) for inst,bars in raw.items()}
    allrows=[];done=0
    colors3=collections.defaultdict(list)
    colors4=collections.defaultdict(list)
    phase=collections.defaultdict(list)
    with_feature=collections.defaultdict(list)
    all_trade_details=[]
    valid_timestamps=[]
    for t in range(START-STEP,END-12*STEP,STEP):
        ranks=[]
        for inst,bars in raw.items():
            bar=bars.get(t);prior=bars.get(t-288*STEP)
            if bar and prior and prior[3]>0:
                ranks.append((bar[3]/prior[3]-1,inst))
        if len(ranks)<200:raise RuntimeError("Incomplete point-in-time TOP200 "+base.when(t))
        ranks.sort(reverse=True)
        enter=t+STEP
        if enter<START:continue
        p=base.period(enter)
        valid_timestamps.append(t)
        for rank,(_,inst) in enumerate(ranks[:200],1):
            bars=raw[inst]
            sig=bars.get(t)
            if sig is None or sig[0]<=0 or sig[3]<=0:continue
            signal_red_pct=100*(1-sig[3]/sig[0])
            if not .4<=signal_red_pct<.8:continue
            ma10=statistics.mean([bars[t-j*STEP][3] for j in range(10)])
            if (sig[3]/ma10-1)*100>-2:continue
            # Only *prior* candle features
            prev=all_previous_features(bars,t,rsi[inst])
            result=base.calc_outcome(bars,enter)
            if result is None:continue
            reason,ret,exit_time=result
            e={"inst":inst,"entry":enter,"exit":exit_time,
               "signal_uk":base.when(t),"entry_uk":base.when(enter),
               "rank":rank,"phase":p,"reason":reason,"ret":ret,
               "signal_red_pct":round(signal_red_pct,4),
               "signal_vol":sig[4],**prev}
            allrows.append(e)
            phase[p].append(e)
            colors3[e["recent_3_colors"]].append(e)
            colors4[e["recent_4_colors"]].append(e)
            for f in e["tags"]:
                with_feature[f].append(e)
        done+=1
        if done%600==0:print("PRECURSOR_PROGRESS="+json.dumps({"completed_timestamps":done,
            "anchor_signal_count":len(allrows)}),flush=True)
    if len(allrows)!=399:
        print("WARNING_PRECURSOR_COUNT="+json.dumps({"current":len(allrows),
                  "expected_previous_research":399}),flush=True)
    print("BASE_PRECURSOR="+json.dumps({"total":summary(allrows),
       "by_phase":{p:summary(phase[p]) for p in ("train","validation","holdout")},
       "full_serial":summary(serial(allrows,START,END))},separators=(",",":")),flush=True)
    # How common is each precursor before TP vs SL, and is it distinct from
    # patterns before SL? 'Time' counts presented separately.
    table=[]
    for tag,rows in with_feature.items():
        a=summary(rows)
        if a["n"]<12:continue
        tp_base=sum(e["reason"]=="TP" for e in allrows)
        sl_base=sum(e["reason"]=="SL" for e in allrows)
        a["before_tp_count"]=a["tp"]
        a["before_sl_count"]=a["sl"]
        a["share_of_all_tp_pct"]=round(100*a["tp"]/tp_base,1) if tp_base else None
        a["share_of_all_sl_pct"]=round(100*a["sl"]/sl_base,1) if sl_base else None
        a["tp_sl_ratio"]=round(a["tp"]/a["sl"],3) if a["sl"] else None
        a["tag"]=tag
        a["train"]=summary([e for e in rows if e["phase"]=="train"])
        a["validation"]=summary([e for e in rows if e["phase"]=="validation"])
        a["holdout"]=summary([e for e in rows if e["phase"]=="holdout"])
        table.append(a)
    table.sort(key=lambda x:(x["net_pct_avg"],x["n"]),reverse=True)

    seq3=[]
    for seq,rows in colors3.items():
        item=summary(rows)
        if item["n"]<5:continue
        item["colors_immediately_before_signal_newest_first"]=seq
        item["holdout"]=summary([e for e in rows if e["phase"]=="holdout"])
        seq3.append(item)
    seq3.sort(key=lambda x:x["n"],reverse=True)
    seq4=[]
    for seq,rows in colors4.items():
        item=summary(rows)
        if item["n"]<5:continue
        item["colors_immediately_before_signal_newest_first"]=seq
        item["holdout"]=summary([e for e in rows if e["phase"]=="holdout"])
        seq4.append(item)
    seq4.sort(key=lambda x:x["n"],reverse=True)

    # Get consistent benchmarks on three chronological partitions.
    slices={"train":(START,TRAINEND),"validation":(TRAINEND,VALIDEND),
            "holdout":(VALIDEND,END)}
    baseline_ser={p:summary(serial(allrows,*window)) for p,window in slices.items()}
    # Find precursors with positive *serial* PNL during train & validation,
    # min 6 serial trades each phase, then check holdout separately.
    selected=[]
    for item in table:
        rows=with_feature[item["tag"]]
        tr=serial(rows,*slices["train"])
        va=serial(rows,*slices["validation"])
        if len(tr)<8 or len(va)<8:continue
        trscore=summary(tr);vascore=summary(va)
        if trscore["net_pct_avg"]<=0 or vascore["net_pct_avg"]<=0:continue
        selected.append({"tag":item["tag"],
            "train":trscore,"validation":vascore,
            "rank_score":min(trscore["net_pct_avg"],vascore["net_pct_avg"])*min(len(tr),len(va))**.25})
    selected.sort(key=lambda x:(x["rank_score"],x["validation"]["net_usdt"]),reverse=True)
    shortlist=[]
    for candidate in selected[:10]:
        tag=candidate["tag"]
        hl=serial(with_feature[tag],*slices["holdout"])
        shortlist.append({**candidate,"holdout":summary(hl),
          "holdout_baseline":baseline_ser["holdout"],
          "serial_last48":summary(serial(
             with_feature[tag],base.tstamp("2026-10-16","04:30"),END)),
          "relative_to_untouched_holdout_baseline_profit_usdt":round(
             summary(hl)["net_usdt"]-baseline_ser["holdout"]["net_usdt"],4)})
    print("PRECURSOR_SELECTION="+json.dumps({"tested_single_precursor_features":len(table),
        "serial_positive_train_validation":len(selected),"tested_patterns":len(seq3),
        "global_untouched_holdout_baseline":baseline_ser["holdout"],
        "shortlist":shortlist},separators=(",",":")),flush=True)
    # Detailed TP vs SL feature distributions. All features are known before
    # confirmation of target red candle.
    typical_features=sorted(
        (x for x in table if x["n"]>=30),
        key=lambda x:(x["before_tp_count"],x["share_of_all_tp_pct"]),
        reverse=True)
    strong_differences=sorted(
        (x for x in table if x["n"]>=25),
        key=lambda x:(x["share_of_all_tp_pct"]-x["share_of_all_sl_pct"]),reverse=True)
    future_price_never_used_to_create_any_prior_tag=True
    report={
        "dates_uk":[base.when(START),base.when(END)],
        "input":f"{len(raw)} markets, 200 strongest 24h price gain at every closed 5m candle",
        "anchor":"5m red body 0.4%-<0.8%, signal close <=SMA10 by 2%; LONG next open, TP1.5 SL1 60min, cost .22",
        "prior_features_only":future_price_never_used_to_create_any_prior_tag,
        "baseline":summary(allrows),"baseline_by_phase":{k:summary(v) for k,v in phase.items()},
        "serial_by_phase":baseline_ser,
        "total_prior_features":len(table),
        "all_precursor_statistics":table,
        "newest_first_3_candle_color_sequences":seq3,
        "newest_first_4_candle_color_sequences":seq4,
        "typical_precursors_before_tp":typical_features[:25],
        "most_distinctive_tp_vs_sl_with_sample_gt25":strong_differences[:30],
        "positive_serial_train_validation_features":len(selected),
        "shortlist_then_final_holdout":shortlist,
        "selected_without_seeing_holdout":True,
        "caution":"All raw signals overlap; serial trades chosen chronologically by historical point-in-time rank. Historic price range 5m OHLC, estimated slippage + fees 0.22%, no book/liquidation/funding. Scanning numerous precursors causes multiple comparison and selection bias; require forward PAPER test. Does not identify deterministic reason for price reversal.",
    }
    Path("research_red_candle_prior_6bars_tp_vs_sl.json").write_text(
        json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    print("TOP_PRECURSORS="+json.dumps([
        {k:v for k,v in x.items() if k in ("tag","n","tp","sl","time","tp_ratio_pct","net_pct_avg","share_of_all_tp_pct","share_of_all_sl_pct")}
        for x in typical_features[:22]],separators=(",",":")),flush=True)
    print("DISTINCTIVE_TP_PRECURSORS="+json.dumps([
        {k:v for k,v in x.items() if k in ("tag","n","tp","sl","time","tp_ratio_pct","net_pct_avg","share_of_all_tp_pct","share_of_all_sl_pct")}
        for x in strong_differences[:25]],separators=(",",":")),flush=True)
    print("PREVIOUS_THREE_COLOR_SEQUENCES="+json.dumps(seq3,separators=(",",":")),flush=True)
    print("PREVIOUS_FOUR_COLOR_SEQUENCES="+json.dumps(seq4,separators=(",",":")),flush=True)
    print("FINAL_PRIOR_CANDLE_AUDIT="+json.dumps({
        "signals":len(allrows),"excluded_coins":excluded,"period":base.when(START)+" to "+base.when(END),
        "TP":sum(e["reason"]=="TP" for e in allrows),"SL":sum(e["reason"]=="SL" for e in allrows),
        "TIME":sum(e["reason"]=="TIME" for e in allrows),
        "feature_count":len(table),"baseline_serial":baseline_ser,
        "shortlisted_serial_features":len(shortlist)},separators=(",",":")),flush=True)

if __name__=="__main__":main()
