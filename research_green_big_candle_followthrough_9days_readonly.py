"""READ ONLY: find causal candle-context rules that improve >=0.8% GREEN 5m LONG.

All 390 currently listed BloFin USDT swaps, 388 with complete OHLCV, TOP200
point-in-time rolling 24h return. Candle bodies >= +0.8%; confirmed signal
then NEXT 5m open LONG. TP +1.5%, SL -1%, max 60 minutes, intrabar SL-first.
Net includes estimated commission+slippage 0.22% roundtrip.

Train 2026-10-09..12 (4d), validation 2026-10-13..15 (3d), untouched
holdout 2026-10-16..18 04:30 (2d+). Search 1- and 2-condition filters
using ONLY train+validation to select. Holdout evaluated at end only.
Single global position serial simulation for selected and baseline strategies.
All archived market OHLCV public. NO change to BloFin live trader.
"""
from __future__ import annotations
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict,Counter
from itertools import combinations
import gzip,json,statistics,math,time

FIVE=300000
UK=ZoneInfo("Europe/London")
def tstamp(day,h):
    return int(datetime.fromisoformat(day+"T"+h+":00+01:00").timestamp()*1000)
START=tstamp("2026-10-09","00:00")
TRAINEND=tstamp("2026-10-13","00:00")
VALIDEND=tstamp("2026-10-16","00:00")
END=tstamp("2026-10-18","04:30")
WARMUP=START-340*FIVE
COST=.22; NOTIONAL=20.
def when(t):return datetime.fromtimestamp(t/1000,UK).strftime("%Y-%m-%d %H:%M")
def period(t):return "train" if t<TRAINEND else "validation" if t<VALIDEND else "holdout"

def load():
    bs={};universe=set()
    for i in range(4):
        with gzip.open(Path("history")/f"shard_{i}.json.gz","rt") as f:x=json.load(f)
        if x["asof_uk"]!="2026-10-17" or x["errors"]:raise RuntimeError("Incomplete eight-day archive")
        universe.update(x["symbols_in_shard"])
        for inst,v in x["candle_rows"].items():
            if inst in bs:raise RuntimeError("Duplicate "+inst)
            bs[inst]={int(k):tuple(b) for k,b in v.items()}
    for folder,prefix,filename,check in (
        ("older","early_hour_","rows","missing"),
        ("recent","rsi12_shard_","candles","failures"),
        ("fresh","candles_48h_","rows","failed")):
        for i in range(4):
            with gzip.open(Path(folder)/f"{prefix}{i}.json.gz","rt") as f:x=json.load(f)
            if x.get(check):raise RuntimeError(f"Missing prices in {folder}, shard {i}")
            for inst,v in x[filename].items():
                if inst not in bs:raise RuntimeError("Market changed "+inst)
                bs[inst].update({int(k):tuple(row) for k,row in v.items()})
    if len(bs)!=390 or len(universe)!=390:raise RuntimeError("Eligible markets !=390")
    excluded={}
    for inst,bars in list(bs.items()):
        # Historical TOP200 requires 24h lookback and daily continuous data.
        missing=sum(t not in bars for t in range(START-288*FIVE,END,FIVE))
        if missing:
            excluded[inst]=missing
            del bs[inst]
    if len(bs)<380:raise RuntimeError("Less than 380 complete markets")
    print("COVERAGE="+json.dumps({"all_current_markets":len(universe),
        "fully_usable":len(bs),"excluded_incomplete":excluded,
        "start":when(START),"train_end":when(TRAINEND),
        "validation_end":when(VALIDEND),"end":when(END)}),flush=True)
    return bs,excluded

def rsi_wilder(bars):
    ks=sorted(k for k in bars if k>=WARMUP and k<END)
    vals=[bars[k][3] for k in ks]
    if len(ks)<30:return {}
    d=[vals[i]-vals[i-1] for i in range(1,len(vals))]
    ups=[max(z,0) for z in d];downs=[max(-z,0) for z in d]
    ag=sum(ups[:14])/14;al=sum(downs[:14])/14
    def r(g,l):
        return (50 if g==0 and l==0 else 100 if l==0 else 100-100/(1+g/l))
    out={ks[14]:r(ag,al)}
    for i in range(15,len(ks)):
        ag=(ag*13+ups[i-1])/14;al=(al*13+downs[i-1])/14
        out[ks[i]]=r(ag,al)
    return out

def features(bars,ts,rank,rsis):
    b=bars[ts];o,h,l,c,v=b
    if o<=0 or c<=0:return []
    last=[bars[ts-j*FIVE] for j in range(1,51)]
    vols=[z[4] for z in last[:20]]
    med=statistics.median(vols)
    if med<=0:return []
    rs=rsis.get(ts)
    if rs is None:return []
    price10=statistics.mean([c]+[x[3] for x in last[:9]])
    price20=statistics.mean([c]+[x[3] for x in last[:19]])
    old10=statistics.mean([x[3] for x in last[3:13]])
    body=100*(c/o-1)
    relv=v/med
    dist10=100*(c/price10-1)
    dist20=100*(c/price20-1)
    prior6=100*(last[0][3]/last[6][3]-1)
    prior12=100*(last[0][3]/last[12][3]-1)
    total12=100*(c/last[11][3]-1)
    pos=(c-l)/(h-l) if h>l else .5
    upwick=(h-c)/(h-l) if h>l else 0
    k14high=max([h]+[z[1] for z in last[:13]])
    k14low=min([l]+[z[2] for z in last[:13]])
    stoch=100*(c-k14low)/(k14high-k14low) if k14high>k14low else 50
    before_green=sum(1 for z in last[:3] if z[3]>z[0])
    vprev=v/last[0][4] if last[0][4]>0 else float("inf")
    names=[]
    def add(name,yes):
        if yes:names.append(name)
    add("RANK_1_20",rank<=20)
    add("RANK_21_50",20<rank<=50)
    add("RANK_51_100",50<rank<=100)
    add("RANK_101_200",rank>100)
    add("RANK_TOP50",rank<=50)
    add("RANK_TOP100",rank<=100)
    hour=datetime.fromtimestamp(ts/1000,UK).hour
    for lo in range(0,24,6):add(f"HOUR_{lo:02d}_{lo+6:02d}",lo<=hour<lo+6)
    add("RSI_LT45",rs<45)
    add("RSI_45_60",45<=rs<60)
    add("RSI_50_65",50<=rs<65)
    add("RSI_55_70",55<=rs<70)
    add("RSI_60_75",60<=rs<75)
    add("RSI_65_80",65<=rs<80)
    add("RSI_70_85",70<=rs<85)
    add("RSI_GE80",rs>=80)
    add("RSI_LT70",rs<70)
    add("RSI_LT75",rs<75)
    add("RSI_GE50",rs>=50)
    add("RSI_RISING_3",rs>rsis.get(ts-3*FIVE,rs))
    add("RSI_FALLING_3",rs<rsis.get(ts-3*FIVE,rs))
    add("MA10_BELOW_MA20",price10<price20)
    add("MA10_ABOVE_MA20",price10>price20)
    add("MA10_SLOPE_UP",price10>old10)
    add("MA10_SLOPE_DOWN",price10<old10)
    add("DIST10_BELOW0",dist10<0)
    add("DIST10_0_0P5",0<=dist10<.5)
    add("DIST10_0_1",0<=dist10<1)
    add("DIST10_0P5_1P5",.5<=dist10<1.5)
    add("DIST10_1_2",1<=dist10<2)
    add("DIST10_GE2",dist10>=2)
    add("DIST10_LT1",dist10<1)
    add("DIST10_LT2",dist10<2)
    add("DIST20_BELOW0",dist20<0)
    add("DIST20_LT1",dist20<1)
    add("DIST20_LT2",dist20<2)
    add("DIST20_GE2",dist20>=2)
    add("RET_PRIOR6_NEGATIVE",prior6<0)
    add("RET_PRIOR6_0_1",0<=prior6<1)
    add("RET_PRIOR6_GE1",prior6>=1)
    add("RET_PRIOR12_NEGATIVE",prior12<0)
    add("RET_PRIOR12_0_1",0<=prior12<1)
    add("RET_PRIOR12_1_3",1<=prior12<3)
    add("RET_PRIOR12_GE3",prior12>=3)
    add("RET_TOTAL12_LT2",total12<2)
    add("RET_TOTAL12_GE3",total12>=3)
    add("PREVIOUS_RED",last[0][3]<last[0][0])
    add("PREVIOUS_GREEN",last[0][3]>last[0][0])
    add("PREV3_GREEN0",before_green==0)
    add("PREV3_GREEN1",before_green==1)
    add("PREV3_GREEN2",before_green==2)
    add("PREV3_GREEN3",before_green==3)
    add("VOL_LT2X",relv<2)
    add("VOL_2_4X",2<=relv<4)
    add("VOL_4_10X",4<=relv<10)
    add("VOL_10_50X",10<=relv<50)
    add("VOL_GE10X",relv>=10)
    add("VOL_GE20X",relv>=20)
    add("VOL_GE50X",relv>=50)
    add("VOL_GE2X",relv>=2)
    add("VOL_LT10X",relv<10)
    add("VOL_RISING",vprev>1)
    add("VOL_RISING_2X",vprev>2)
    add("VOL_RISING_5X",vprev>5)
    add("VOL_FALLING",vprev<1)
    add("BODY_0P8_1P2",.8<=body<1.2)
    add("BODY_1P2_2",1.2<=body<2)
    add("BODY_GE2",body>=2)
    add("BODY_LT1P5",body<1.5)
    add("CLOSE_TOP_10PCT",pos>=.9)
    add("CLOSE_TOP_20PCT",pos>=.8)
    add("CLOSE_MIDDLE",.35<=pos<.8)
    add("UPPER_WICK_GE20PCT",upwick>=.2)
    add("UPPER_WICK_LT10PCT",upwick<.1)
    add("STOCH_K_LT50",stoch<50)
    add("STOCH_K_50_80",50<=stoch<80)
    add("STOCH_K_80_95",80<=stoch<95)
    add("STOCH_K_GE95",stoch>=95)
    add("BREAKOUT_PRIOR6",c>max(z[1] for z in last[:6]))
    add("NOT_BREAKOUT_PRIOR6",c<=max(z[1] for z in last[:6]))
    add("ENGULF_BULL",last[0][3]<last[0][0] and o<=last[0][3] and c>=last[0][0])
    add("BREAK_PRIOR_CLOSE",c>last[0][3])
    add("ATR_14_HI",statistics.mean(z[1]-z[2] for z in last[:14])/c>=.015)
    add("ATR_14_LOW",statistics.mean(z[1]-z[2] for z in last[:14])/c<.015)
    return names

def calc_outcome(bars,entry):
    b=bars[entry]
    price=b[0]
    stop=price*.99;target=price*1.015
    for j in range(12):
        k=entry+j*FIVE
        bar=bars.get(k)
        if bar is None:return None
        tp=bar[1]>=target;sl=bar[2]<=stop
        if sl:return ("SL",-1.22,k+FIVE)
        if tp:return ("TP",1.28,k+FIVE)
    close=bars[entry+11*FIVE][3]
    return ("TIME",100*(close/price-1)-COST,entry+12*FIVE)

def metric(events):
    n=len(events)
    if not n:return {"n":0,"tp":0,"sl":0,"time":0,"net_pct_sum":0,"mean_net_pct":None,"tp_rate":0}
    s=sum(e["ret"] for e in events)
    return {"n":n,"tp":sum(x["reason"]=="TP" for x in events),
         "sl":sum(x["reason"]=="SL" for x in events),
         "time":sum(x["reason"]=="TIME" for x in events),
         "wins_after_cost":sum(x["ret"]>0 for x in events),
         "net_pct_sum":round(s,4),"mean_net_pct":round(s/n,5),
         "tp_rate":round(100*sum(x["reason"]=="TP" for x in events)/n,2),
         "net_usdt_per_20":round(s*.2,4)}
def serial(events):
    events=sorted(events,key=lambda e:(e["entry"],e["rank"],e["inst"]))
    last=-1;positions=[]
    for e in events:
        if e["entry"]>=last:
            positions.append(e);last=e["exit"]
    return metric(positions)

def main():
    raw,excluded=load()
    rsis={s:rsi_wilder(bars) for s,bars in raw.items()}
    cand=[]
    feature_set=defaultdict(lambda:{"train":set(),"validation":set(),"holdout":set()})
    per={"train":[],"validation":[],"holdout":[]}
    coverage=[]
    count=0
    for t in range(START-FIVE,END-12*FIVE,FIVE):
        rank=[]
        for s,bars in raw.items():
            b=bars.get(t);old=bars.get(t-288*FIVE)
            if b is not None and old is not None and old[3]>0:
                rank.append((b[3]/old[3]-1,s))
        if len(rank)<200:raise RuntimeError(f"Insufficient TOP200 ranking {when(t)} {len(rank)}")
        rank.sort(reverse=True)
        coverage.append(len(rank))
        entry=t+FIVE
        if entry<START:continue
        part=period(entry)
        for rank_i,(_,inst) in enumerate(rank[:200],1):
            bars=raw[inst]
            b=bars.get(t)
            if b is None or b[0]<=0 or 100*(b[3]/b[0]-1)<.8:continue
            feat=features(bars,t,rank_i,rsis[inst])
            if not feat:continue
            out=calc_outcome(bars,entry)
            if out is None:continue
            reason,pnl,exit_ms=out
            idx=len(cand)
            e={"inst":inst,"entry":entry,"exit":exit_ms,"day":datetime.fromtimestamp(entry/1000,UK).date().isoformat(),
               "part":part,"rank":rank_i,"ret":pnl,"reason":reason}
            cand.append(e)
            per[part].append(idx)
            for f in feat:feature_set[f][part].add(idx)
        count+=1
        if count%500==0:print("SCAN_PROGRESS="+json.dumps({"minute":count,"total":len(cand),"train":len(per["train"]),"validation":len(per["validation"]),"holdout":len(per["holdout"])}),flush=True)
    print("BASELINE_PER_PERIOD="+json.dumps({p:{"all":metric([cand[i] for i in inds]),"serial":serial([cand[i] for i in inds])} for p,inds in per.items()},separators=(",",":")),flush=True)
    filters=sorted(feature_set)
    print("FEATURES="+json.dumps({"n":len(filters),"names":filters}),flush=True)
    best=[]
    for pair in [(f,) for f in filters]+list(combinations(filters,2)):
        st=[feature_set[p]["train"] for p in pair]
        indices=set.intersection(*st) if len(st)==2 else st[0]
        if len(indices)<35:continue
        train=metric([cand[i] for i in indices])
        if train["mean_net_pct"]<=0:continue
        vv=[feature_set[p]["validation"] for p in pair]
        validids=set.intersection(*vv) if len(vv)==2 else vv[0]
        if len(validids)<25:continue
        valid=metric([cand[i] for i in validids])
        if valid["mean_net_pct"]<=0:continue
        # No holdout accesses in the selection loop.
        best.append({"filter":pair,"train":train,"validation":valid,
             "score":min(train["mean_net_pct"],valid["mean_net_pct"])*min(len(indices),len(validids))**.25})
    best.sort(key=lambda a:(a["score"],a["validation"]["mean_net_pct"]),reverse=True)
    selected=best[:20]
    print("SELECTION_COUNTS="+json.dumps({"filters_tested":len(filters),"rules_tested":len(filters)+len(filters)*(len(filters)-1)//2,
        "rules_positive_train_and_validation_min_n":len(best),"shortlist":len(selected)}),flush=True)
    output=[]
    for x in selected:
        pair=x["filter"]
        def grab(p):
            ids=[feature_set[f][p] for f in pair]
            return [cand[i] for i in (set.intersection(*ids) if len(ids)==2 else ids[0])]
        record={"filter":list(pair),"train":x["train"],
                "validation":x["validation"],"holdout":metric(grab("holdout")),
                "serial_train":serial(grab("train")),
                "serial_validation":serial(grab("validation")),
                "serial_holdout":serial(grab("holdout"))}
        output.append(record)
    # Also assess explicit complementary hypothesis (strong move reversal).
    report={
      "period_uk":[when(START),when(END)],
      "phase_boundaries_uk":[when(TRAINEND),when(VALIDEND)],
      "universe":len(raw),"excluded_coins":excluded,
      "rank_snapshots":len(coverage),
      "signals_total":len(cand),
      "setup":"After GREEN 5m body >=0.8% LONG on next open; TP +1.5%, SL -1%, max 60m; 0.22% roundtrip cost.",
      "methodology":"Each signal outcome independently measured even if overlapping; single-position nonoverlap also presented. Causal features and ranking; feature rules chosen on train AND validation, unseen holdout evaluated after selection. Research only, no LIVE changes. Historical point-in-time TOP200 based on all currently listed 388 complete-history USDT swap coins. Entry after confirmed close, SL-first same bar. Holding at most 60m; last hour excluded; funding not modeled.",
      "unfiltered":{
       p:{"all":metric([cand[i] for i in inds]),"serial":serial([cand[i] for i in inds])}
       for p,inds in per.items()},
      "rules_tested":len(filters)+len(filters)*(len(filters)-1)//2,
      "rules_positive_train_and_validation":len(best),
      "candidate_list":output
    }
    Path("blofin_green_body08_extra_conditions_research.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("FINAL_COUNT="+json.dumps({k:v for k,v in report.items() if k not in ("candidate_list","unfiltered")},separators=(",",":")),flush=True)
    for p in ["train","validation","holdout"]:
        print("BASELINE_"+p.upper()+"="+json.dumps(report["unfiltered"][p],separators=(",",":")),flush=True)
    print("WINNING_RULES="+json.dumps(output,separators=(",",":")),flush=True)

if __name__=="__main__":main()
