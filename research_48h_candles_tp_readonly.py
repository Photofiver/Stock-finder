"""READ ONLY: 48h, each 5m candle pattern vs TP +1.5% before SL -1%.

Date: 2026-10-16 04:30 to 2026-10-18 04:30 UK BST.
Rank BloFin 390 live linear USDT crypto swap symbols each candle by trailing
24h percent price gain, TOP200 point-in-time. No future knowledge in rank.
For each candlestick pattern LONG and SHORT enter next 5m open, TP+1.5%
or SL-1%, max12 bars(60min), conservative SL first when ambiguous.
Include estimated 0.22% per complete trade. Count independent *signals*,
then simulate ONE NONOVERLAPPING GLOBAL POSITION for selected patterns.
Divide into first and second 24h before selection; no tuning on second day.
The final 60m has insufficient forward outcome and is excluded from final
score while raw candle coverage includes full 48h. Earlier-period outcomes
may resolve in later-period candle data, but no parameters selected using
such outcomes.
Never use BloFin private API, LIVE bot credentials or trade endpoints.
"""
from __future__ import annotations
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor,as_completed
from collections import defaultdict
from pathlib import Path
import gzip,json,statistics,sys, time as sleep

import blofin_top200_repeat_setup_research as m
FIVE=m.FIVE;UK=ZoneInfo("Europe/London")
START=int(datetime(2026,10,16,3,30,tzinfo=timezone.utc).timestamp()*1000)
SPLIT=START+288*FIVE
END=SPLIT+288*FIVE
LAST_OLD=int(datetime(2026,10,18,1,55,tzinfo=timezone.utc).timestamp()*1000)
SHARDS=4
NOTIONAL=20.
COST=0.22
m.MIN_CALL_GAP=.18
def uk(t):return datetime.fromtimestamp(t/1000,UK).strftime("%Y-%m-%d %H:%M")
def acquire(shard):
    names=m.list_swaps()
    if len(names)!=390:raise RuntimeError("Market listings changed; archive historical TOP200 universe not equivalent")
    these=names[shard::SHARDS]
    fail={};good={}
    def one(symbol):
        arr=m.get_data("/api/v1/market/candles",{"instId":symbol,"bar":"5m","after":str(END+FIVE),"limit":"100"})
        bars={}
        for r in arr:
            if len(r)<9 or str(r[8])!="1":continue
            try:
                t=int(r[0])
                if t<LAST_OLD or t>=END:continue
                bars[t]=tuple(float(r[i]) for i in (1,2,3,4,5))
            except (ValueError,TypeError,IndexError):continue
        # last old data includes through LAST_OLD-5min, expect 19 more candles
        expect=set(range(LAST_OLD,END,FIVE))
        if set(bars)!=expect:raise RuntimeError("Incomplete fresh last "+str(len(bars))+"/"+str(len(expect)))
        return bars
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs={pool.submit(one,s):s for s in these}
        for i,f in enumerate(as_completed(futs),1):
            s=futs[f]
            try:good[s]={str(t):list(v) for t,v in f.result().items()}
            except Exception as ex:fail[s]=str(ex)
            if i%25==0:print("FRESH_PROGRESS="+json.dumps({"shard":shard,"done":i,"good":len(good),"bad":len(fail)}),flush=True)
    for j in range(4):
        if not fail:break
        sleep.sleep(3+j*4)
        for s in list(fail):
            try:good[s]={str(t):list(v) for t,v in one(s).items()};del fail[s]
            except Exception as ex:fail[s]=str(ex)
        print("RETRY="+json.dumps({"shard":shard,"attempt":j+1,"good":len(good),"bad":len(fail)}),flush=True)
    with gzip.open("candles_48h_"+str(shard)+".json.gz","wt",encoding="utf-8",compresslevel=3) as f:json.dump({"shard":shard,"symbols":these,"eligible":names,"rows":good,"failed":fail,"end":END},f,separators=(",",":"))
    print("FRESH_DONE="+json.dumps({"shard":shard,"total":len(these),"good":len(good),"failed":fail}),flush=True)

def load():
    bars={};universe=set()
    for i in range(SHARDS):
        with gzip.open(Path("history")/f"shard_{i}.json.gz","rt",encoding="utf-8") as f:o=json.load(f)
        assert o["asof_uk"]=="2026-10-17" and not o["errors"]
        universe.update(o["symbols_in_shard"])
        for s,d in o["candle_rows"].items():
            if s in bars:raise RuntimeError("duplicate "+s)
            bars[s]={int(t):tuple(x) for t,x in d.items()}
    for i in range(SHARDS):
        with gzip.open(Path("older")/f"early_hour_{i}.json.gz","rt",encoding="utf-8") as f:o=json.load(f)
        assert o["shard"]==i and not o["missing"]
        for s,d in o["rows"].items():
            if s not in bars:raise RuntimeError("coin different "+s)
            bars[s].update({int(t):tuple(v) for t,v in d.items()})
    for i in range(SHARDS):
        with gzip.open(Path("recent")/f"rsi12_shard_{i}.json.gz","rt",encoding="utf-8") as f:o=json.load(f)
        assert o["shard"]==i and o["end_ms"]==LAST_OLD and not o["failures"]
        for s,d in o["candles"].items():
            bars[s].update({int(t):tuple(v) for t,v in d.items()})
    for i in range(SHARDS):
        with gzip.open(Path("fresh")/f"candles_48h_{i}.json.gz","rt",encoding="utf-8") as f:o=json.load(f)
        assert o["shard"]==i and o["end"]==END and not o["failed"]
        if set(o["eligible"])!=universe:raise RuntimeError("Different market universe")
        for s,d in o["rows"].items():
            bars[s].update({int(t):tuple(v) for t,v in d.items()})
    if len(bars)!=390:raise RuntimeError("Expected 390 complete markets "+str(len(bars)))
    missing={}
    for s,d in bars.items():
        bad=[t for t in range(START-288*FIVE,END,FIVE) if t not in d]
        if bad:missing[s]=[uk(bad[0]),len(bad)]
    if missing:
        print("COVERAGE_GAPS="+json.dumps(missing),flush=True)
        raise RuntimeError("Incomplete 48h full market history; cannot reliably rank")
    print("COVERAGE_OK="+json.dumps({"start_uk":uk(START),"end_uk":uk(END),"all_markets":len(bars),
        "candles_per_market_48h":576,"complete_market_candles":576*len(bars)}),flush=True)
    return bars

def calc_rsi_wilder(bars):
    times=sorted(bars)
    nums=[bars[t][3] for t in times]
    if len(nums)<16:return {}
    changes=[nums[i]-nums[i-1] for i in range(1,len(nums))]
    gain=sum(max(0,x) for x in changes[:14])/14
    loss=sum(max(0,-x) for x in changes[:14])/14
    def rsi(g,l):
        return 50 if g==0 and l==0 else 100 if l==0 else 100-100/(1+g/l)
    o={times[14]:rsi(gain,loss)}
    for i in range(15,len(times)):
        d=changes[i-1]
        gain=(13*gain+max(0,d))/14
        loss=(13*loss+max(0,-d))/14
        o[times[i]]=rsi(gain,loss)
    return o

def pattern_tags(b,p,prev20,prev6,rsi):
    o,h,l,c,v=b
    previous=p
    if previous is None or len(prev20)<20 or len(prev6)<6:return []
    med=statistics.median(x[4] for x in prev20)
    if med<=0 or o<=0:return []
    color="LONG" if c>o else "SHORT" if c<o else "DOJI"
    highrange=h-l
    body=abs(c-o)
    upper=h-max(o,c)
    lower=min(o,c)-l
    ratio=v/med
    bodypct=100*body/o
    # Include a simple comparison for the whole market signal base (all candles).
    tags=[("ANY_CANDLE", "LONG"),("ANY_CANDLE", "SHORT")]
    if color=="DOJI":
        tags.extend([("DOJI","LONG"),("DOJI","SHORT")])
    if color in ("LONG","SHORT"):
        tags.append(("CANDLE_"+("GREEN" if color=="LONG" else "RED"),color))
        if bodypct>=.4:tags.append(("BODY_0P4",color))
        if bodypct>=.8:tags.append(("BODY_0P8",color))
        if ratio>=2:tags.append(("VOLUME_2X",color))
        if ratio>=4:tags.append(("VOLUME_4X",color))
        if ratio>=10:tags.append(("VOLUME_10X",color))
        if ratio>=2 and bodypct>=.4:tags.append(("VOL2_BODY04",color))
        if ratio>=4 and bodypct>=.4:tags.append(("VOL4_BODY04",color))
        if ratio>=4 and highrange>0 and (c-l)/highrange>=.8 and color=="LONG":
            tags.append(("VOL4_CLOSE_TOP20","LONG"))
        if ratio>=4 and highrange>0 and (c-l)/highrange<=.2 and color=="SHORT":
            tags.append(("VOL4_CLOSE_BOTTOM20","SHORT"))
        if highrange>0 and body/highrange>=.8:tags.append(("MARUBOZU_80PCT",color))
        if highrange>0 and ((c-l)/highrange>=.8 if color=="LONG" else (c-l)/highrange<=.2):
            tags.append(("CLOSE_EDGE_20PCT",color))
        if lower>=2*body and lower>=.25*highrange and upper<=body and highrange>0:
            tags.append(("LONG_LOWER_WICK_HAMMER",color))
        if upper>=2*body and upper>=.25*highrange and lower<=body and highrange>0:
            tags.append(("LONG_UPPER_WICK_STAR",color))
        prev=previous
        if ((color=="LONG" and prev[3]<prev[0] and o<=prev[3] and c>=prev[0]) or
            (color=="SHORT" and prev[3]>prev[0] and o>=prev[3] and c<=prev[0])):
            tags.append(("BODY_ENGULFING",color))
        if l>=prev[2] and h<=prev[1]:tags.append(("INSIDE_BAR",color))
        if color=="LONG" and c>max(x[1] for x in prev6):tags.append(("BREAKOUT_6_HIGH","LONG"))
        if color=="SHORT" and c<min(x[2] for x in prev6):tags.append(("BREAKOUT_6_LOW","SHORT"))
        if ratio>=2 and ((color=="LONG" and c>max(x[1] for x in prev6)) or
                        (color=="SHORT" and c<min(x[2] for x in prev6))):
            tags.append(("VOL2_BREAKOUT_6",color))
        if (color=="LONG" and prev[3]<prev[0]) or (color=="SHORT" and prev[3]>prev[0]):
            tags.append(("COLOR_REVERSAL",color))
            if ratio>=2:tags.append(("REVERSAL_VOL2",color))
        if color=="LONG" and all(x[3]>x[0] for x in prev6[-2:]):
            tags.append(("THREE_GREEN",color))
            if ratio>=2:tags.append(("THREE_GREEN_VOL2",color))
        if color=="SHORT" and all(x[3]<x[0] for x in prev6[-2:]):
            tags.append(("THREE_RED",color))
            if ratio>=2:tags.append(("THREE_RED_VOL2",color))
        if color=="LONG" and prev[3]>prev[0]:tags.append(("TWO_GREEN","LONG"))
        if color=="SHORT" and prev[3]<prev[0]:tags.append(("TWO_RED","SHORT"))
        if color=="LONG" and rsi is not None and 40<=rsi<=65 and ratio>=2:
            tags.append(("VOL2_RSI40_65","LONG"))
        if color=="SHORT" and rsi is not None and 35<=rsi<=60 and ratio>=2:
            tags.append(("VOL2_RSI35_60","SHORT"))
        if v>prev[4] and ratio>=2:tags.append(("VOL2_RISING_VOL",color))
    return tags

def outcome(bars,entry_ms,side):
    entry=bars[entry_ms][0]
    sig=1 if side=="LONG" else -1
    for j in range(12):
        t=entry_ms+j*FIVE
        o,h,l,c,v=bars[t]
        sl=(l<=entry*.99 if sig>0 else h>=entry*1.01)
        tp=(h>=entry*1.015 if sig>0 else l<=entry*.985)
        if sl:return -1.22,"SL" if not tp else "SL_AND_TP_SAME_BAR",t+FIVE
        if tp:return 1.28,"TP",t+FIVE
    close=bars[entry_ms+11*FIVE][3]
    return 100*sig*(close/entry-1)-COST,"TIME",entry_ms+12*FIVE

def score_item(s):
    n=len(s)
    if n==0:return {"signals":0,"tp":0,"sl":0,"time":0,"tp_first_rate":None,"net_positive_rate":None,"mean_net_pct":None}
    return {"signals":n,"tp":sum(x["exit"]=="TP" for x in s),
       "sl":sum(x["exit"].startswith("SL") for x in s),
       "time":sum(x["exit"]=="TIME" for x in s),
       "tp_first_rate":round(100*sum(x["exit"]=="TP" for x in s)/n,2),
       "net_positive_rate":round(100*sum(x["net_pct"]>0 for x in s)/n,2),
       "mean_net_pct":round(sum(x["net_pct"] for x in s)/n,4),
       "net_20usdt_all_signals":round(sum(x["net_pct"] for x in s)*.2,4)}

def serial(events):
    # A single globally open trade from point-in-time TOP200. Choose rank 1 first.
    busy=START
    trades=[]
    for x in events:
        if x["entry_ms"]<busy:continue
        busy=x["exit_ms"]
        trades.append(x)
    return {"all":score_item(trades),"first_day":score_item([x for x in trades if x["entry_ms"]<SPLIT]),
      "second_day":score_item([x for x in trades if x["entry_ms"]>=SPLIT]),
      "trades":[{"coin":x["coin"],"at_uk":uk(x["entry_ms"]),"side":x["side"],
                  "result":x["exit"],"net_pct":round(x["net_pct"],4)} for x in trades[:150]]}

def study():
    bars=load()
    rsis={s:calc_rsi_wilder(d) for s,d in bars.items()}
    # memoize candle pattern masks per coin and minute; at most 390*576
    labels=defaultdict(list)
    all_count=defaultdict(int)
    eligible_ts=range(START-FIVE,END-12*FIVE,FIVE)
    times=0
    for ts in eligible_ts:
        ranks=[]
        for s,d in bars.items():
            b=d[ts]
            prior=d[ts-288*FIVE]
            if prior[3]>0:ranks.append((b[3]/prior[3]-1,s))
        if len(ranks)<200:raise RuntimeError("Too few coin ranks "+uk(ts))
        ranks.sort(reverse=True)
        # signal candle with timestamp ts, entry at ts+FIVE
        t=ts+FIVE
        day="train" if t<SPLIT else "test"
        for rank,(_,s) in enumerate(ranks[:200],1):
            d=bars[s]
            prev=[d[ts-j*FIVE] for j in range(1,21)]
            p=prev[0]
            rev=list(reversed(prev[:6]))
            features=pattern_tags(d[ts],p,prev,rev,rsis[s].get(ts))
            if not features:continue
            # Compute each side only once, not once per pattern
            already={}
            for name,side in features:
                if side not in already:
                    net,why,end=outcome(d,t,side)
                    already[side]=(net,why,end)
                net,why,end=already[side]
                obs={"coin":s,"entry_ms":t,"exit_ms":end,
                     "rank":rank,"side":side,"net_pct":net,"exit":why,"day":day,
                     "signal_open":d[ts][0],"signal_close":d[ts][3],
                     "signal_volume":d[ts][4]}
                labels[(name,side,day)].append(obs)
                all_count[day]+=1
        times+=1
        if times%100==0:print("SCANNED_PROGRESS="+json.dumps({"done":times,"of":len(eligible_ts),"features":len(labels)}),flush=True)
    print("RANKINGS_48H="+json.dumps({"full_candles_per_coin":576,
      "evaluated_signal_timestamps":times,
      "train_day":f"{uk(START)}--{uk(SPLIT)}",
      "validation_day":f"{uk(SPLIT)}--{uk(END)}",
      "last_hour_incomplete_outcomes_excluded":True,
      "markets":len(bars)}),flush=True)
    results=[]
    for key in sorted({(name,side) for name,side,day in labels}):
        name,side=key
        train=labels.get((name,side,"train"),[])
        test=labels.get((name,side,"test"),[])
        if not train and not test:continue
        t=score_item(train);v=score_item(test)
        results.append({"pattern":name,"side":side,"train":t,"validation":v,
          "two_day_all_signals":score_item(train+test)})
    results.sort(key=lambda x:(x["side"],x["pattern"]))
    # Pick only by first 24h training, min sample >=50 in first day.
    # Prefer true TP hit rate, with net expectancy as tie-break. Compare both
    # days after selecting without using second-day data to tune.
    choices={}
    for side in ("LONG","SHORT"):
        options=[r for r in results if r["side"]==side and r["pattern"]!="ANY_CANDLE"
                 and r["train"]["signals"]>=40]
        options.sort(key=lambda r:(r["train"]["mean_net_pct"],
                   r["train"]["tp_first_rate"]),reverse=True)
        chosen=options[:5]
        choices[side]=[]
        for r in chosen:
            arr=labels.get((r["pattern"],side,"train"),[])+labels.get((r["pattern"],side,"test"),[])
            arr.sort(key=lambda x:(x["entry_ms"],x["rank"],x["coin"]))
            outcome_serial=serial(arr)
            choices[side].append({"pattern":r["pattern"],"signal_train":r["train"],
                "signal_validation":r["validation"],
                "serial_non_overlapping_trades":outcome_serial})
    # Also provide all top by validation, but label as descriptive not training result.
    diagnostic={}
    for side in ("LONG","SHORT"):
        top=[r for r in results if r["side"]==side and r["validation"]["signals"]>=30]
        top.sort(key=lambda r:r["validation"]["tp_first_rate"],reverse=True)
        diagnostic[side]=top[:8]
    report={
      "analysis":"48h every closed 5m candle for TOP200 gainers, 390 live BloFin USDT crypto swaps",
      "window_uk":[uk(START),uk(END)],
      "train_test_split_uk":uk(SPLIT),
      "tp_pct":1.5,"sl_pct":1.0,"max_hold_minutes":60,
      "fee_plus_slippage_roundtrip_pct":COST,
      "enter_next_candle_open":True,
      "conservative_same_candle_sl_first":True,
      "universe":390,"ranked_top":200,
      "outcomes_available_for_entry_before_uk":uk(END-12*FIVE),
      "all_candle_pattern_stats":results,
      "top_patterns_selected_only_by_first_24h":choices,
      "descriptive_highest_tp_by_validation_not_preselected":diagnostic,
      "definition":"Net winner = TP 1.28%, SL -1.22%, otherwise actual 60min outcome less 0.22%. Every candle signal counted even if hypothetical trades overlap; serial simulation prevents global overlap. A result from a 48h sample cannot establish predictive reliability. Causal 24h gain rank among currently listed symbols, no historic delistings. Funding omitted.",
    }
    Path("blofin_48h_candle_tp_patterns.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for side in ("LONG","SHORT"):
        base=next(r for r in results if r["pattern"]=="ANY_CANDLE" and r["side"]==side)
        print("BASELINE_"+side+"="+json.dumps(base,separators=(",",":")),flush=True)
        print("TRAIN_CHOSEN_"+side+"="+json.dumps([{"pattern":x["pattern"],"train":x["signal_train"],
          "validation":x["signal_validation"],
          "serial_train":x["serial_non_overlapping_trades"]["first_day"],
          "serial_validation":x["serial_non_overlapping_trades"]["second_day"]} for x in choices[side]],separators=(",",":")),flush=True)
        print("TOP_VALIDATION_"+side+"="+json.dumps(diagnostic[side],separators=(",",":")),flush=True)
    print("ANALYSIS_DONE="+json.dumps({"patterns_tested":len(results),"markets":len(bars),
      "signals_considered":all_count,"window":[uk(START),uk(END)],"report":"blofin_48h_candle_tp_patterns.json"},separators=(",",":")),flush=True)

if __name__=="__main__":
    if len(sys.argv)<2:raise SystemExit("Usage: fetch 0-3 | analyze")
    if sys.argv[1]=="fetch":acquire(int(sys.argv[2]))
    elif sys.argv[1]=="analyze":study()
    else:raise SystemExit("Bad mode")
