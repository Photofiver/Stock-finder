#!/usr/bin/env python3
"""READ ONLY: research best volume filters for BINANCE SPOT BATUSDT 5m.
280 combinations, chronological 70/30 holdout. No credentials, no broker calls.
Official BinanceVision archive data only. Writes own research JSON.
"""
import collections
import concurrent.futures
import csv
import datetime as dt
import io
import json
import statistics
import time
import urllib.request
import zipfile
from pathlib import Path

UTC=dt.timezone.utc
FIVE=300000
SYM="BATUSDT"
MINIMUMS=(0,25000,50000,75000,100000,125000,150000,200000)
MULTIPLES=(0,2,3,4,5,7,10)
MEMORIES=(24,48,72,96,0)
TP,SL,FEE=.01,.0025,.0006
SIZE=20.0
MIN_TRAIN_TRADES=20

def fetch_archive(arg):
    kind,name=arg
    uri=f"https://data.binance.vision/data/spot/{kind}/klines/{SYM}/5m/{SYM}-5m-{name}.zip"
    try:
        req=urllib.request.Request(uri,headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req,timeout=40) as r:
            body=r.read()
        rows=[]
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            csv_name=next(x for x in z.namelist() if x.endswith(".csv"))
            with z.open(csv_name) as content:
                for row in csv.reader(io.TextIOWrapper(content,encoding="utf-8")):
                    try:
                        ts=int(row[0]);ts=ts//1000 if ts>10**14 else ts
                        o,h,l,c,v=(float(row[j]) for j in (1,2,3,4,5))
                        if ts%FIVE==0 and min(o,h,l,c)>0 and v>=0:
                            rows.append((ts,o,h,l,c,v))
                    except (ValueError,TypeError,IndexError):
                        continue
        return name,rows,None
    except Exception as e:
        return name,[],str(e)[:220]

def source_data():
    now=dt.datetime.now(UTC)
    end=now.replace(hour=0,minute=0,second=0,microsecond=0)
    start=end-dt.timedelta(days=92)
    cursor=start.replace(day=1)
    tasks=[]
    while cursor<end:
        next_month=(cursor+dt.timedelta(days=35)).replace(day=1)
        if next_month<=end:
            tasks.append(("monthly",cursor.strftime("%Y-%m")))
        else:
            day=cursor
            while day<end:
                if day>=start:
                    tasks.append(("daily",day.strftime("%Y-%m-%d")))
                day+=dt.timedelta(days=1)
        cursor=next_month
    print("ARCHIVES_REQUESTED",len(tasks),"END",end.isoformat(),flush=True)
    store={}
    problems=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs=[pool.submit(fetch_archive,x) for x in tasks]
        for f in concurrent.futures.as_completed(futs):
            name,records,err=f.result()
            if err:
                problems.append({"archive":name,"error":err})
                print("ARCHIVE_FAILED",name,err,flush=True)
            else:
                print("ARCHIVE_OK",name,len(records),flush=True)
                for x in records:
                    if int(start.timestamp()*1000)<=x[0]<int(end.timestamp()*1000):
                        store[x[0]]=x
    bars=[store[k] for k in sorted(store)]
    if len(bars)<5000:
        raise RuntimeError(f"Insufficient authoritative 5m history: {len(bars)} bars")
    return bars,end,problems

def summarize(trades,excluded,anomalies,signals):
    net=sum(x[0] for x in trades)
    gp=sum(max(0,x[0]) for x in trades)
    gl=sum(max(0,-x[0]) for x in trades)
    equity=0.;high=0.;maxdd=0.
    for p,_,_ in trades:
        equity+=p
        high=max(high,equity)
        maxdd=max(maxdd,high-equity)
    n=len(trades)
    return {"trades":n,"wins":sum(x[0]>0 for x in trades),"losses":sum(x[0]<0 for x in trades),
            "win_pct":round(100*sum(x[0]>0 for x in trades)/n,2) if n else None,
            "net_usdt":round(net,5),"profit_factor":round(gp/gl,3) if gl else ("inf" if gp else None),
            "max_drawdown_usdt":round(maxdd,5),
            "long_net_usdt":round(sum(x[0] for x in trades if x[1]==1),5),
            "short_net_usdt":round(sum(x[0] for x in trades if x[1]==-1),5),
            "same_bar_tp_sl_counted_as_loss":sum(x[2] for x in trades),
            "excluded_bars":excluded,"anomaly_bars":anomalies,"trend_flips":signals}

def backtest(bars,end,days,allowed):
    endms=int(end.timestamp()*1000)
    start=endms-days*86400000
    split=start+int(days*.7*86400000//FIVE)*FIVE
    warm=start-60*FIVE
    data=[b for b in bars if warm<=b[0]<endms]
    if len(data)<60 or data[0][0]>start-50*FIVE:
        return {"days":days,"error":"At least 50 preceding 5m candles unavailable"}
    med=[None]*len(data)
    for i in range(50,len(data)):
        med[i]=statistics.median(b[5] for b in data[i-50:i])
    bad=[False]*len(data)
    for i in range(1,len(data)):
        if data[i][0]-data[i-1][0]!=FIVE:
            bad[i]=True
    combos=[]
    for floor in MINIMUMS:
      for factor in MULTIPLES:
       for memory in MEMORIES:
        trend=0;record=0.;dq=collections.deque()
        busy=-1
        train=[];test=[]
        rejected=[0,0];anomalies=[0,0];flips=[0,0]
        for i in range(50,len(data)-2):
            ts,o,h,l,c,v=data[i]
            part=0 if ts<split else 1
            if ts<start:part=-1
            if bad[i]:
                trend=0;record=0.;dq.clear();busy=-1
            if memory:
                cut=i-memory+1
                while dq and dq[0][0]<cut:dq.popleft()
                record=dq[0][1] if dq else 0.
            median=med[i]
            anomaly=bool(factor and median and v>factor*median)
            rejected_now=anomaly or v<floor or c==o
            if part>=0:
                rejected[part]+=int(rejected_now)
                anomalies[part]+=int(anomaly)
            if rejected_now:
                continue
            direction=1 if c>o else -1
            flip=trend!=0 and direction!=trend and v>record
            if trend==0 or flip:
                trend=direction
                record=0.
                dq.clear()
            if trend==direction:
                if memory==0:
                    record=max(record,v)
                else:
                    while dq and dq[-1][1]<=v:
                        dq.pop()
                    dq.append((i,v))
                    record=dq[0][1]
            if not flip or part<0:continue
            flips[part]+=1
            if direction!=allowed:continue
            if i<busy:continue
            x=data[i+1];y=data[i+2]
            # Never create candles or fill across gaps.
            if x[0]!=ts+FIVE or y[0]!=ts+2*FIVE:continue
            boundary=split if part==0 else endms
            if y[0]+FIVE>boundary:continue
            entry=x[1]
            if entry<=0:continue
            tp=entry*(1+direction*TP);sl=entry*(1-direction*SL)
            px=y[4];which=i+2;collision=False
            for j,z in enumerate((x,y)):
                op,hi,lo=z[1],z[2],z[3]
                if (op<=sl if direction==1 else op>=sl):
                    px=op;which=i+1+j;break
                if (op>=tp if direction==1 else op<=tp):
                    px=op;which=i+1+j;break
                stop=(lo<=sl if direction==1 else hi>=sl)
                take=(hi>=tp if direction==1 else lo<=tp)
                if stop:
                    px=sl;which=i+1+j;collision=take;break
                if take:
                    px=tp;which=i+1+j;break
            gross=SIZE*direction*(px/entry-1)
            fees=FEE*SIZE*(1+px/entry)
            (train if part==0 else test).append((gross-fees,direction,collision))
            busy=which
        combos.append({"minimum_volume":floor,"anomaly_multiple":factor,"memory_bars":memory,
                       "train":summarize(train,rejected[0],anomalies[0],flips[0]),
                       "test":summarize(test,rejected[1],anomalies[1],flips[1])})
    qualified=sorted([a for a in combos if a["train"]["trades"]>=MIN_TRAIN_TRADES],
                     key=lambda a:(-a["train"]["net_usdt"],-a["train"]["trades"],
                                  a["minimum_volume"],a["anomaly_multiple"],a["memory_bars"]))
    baseline=next(c for c in combos if c["minimum_volume"]==0 and c["anomaly_multiple"]==0 and c["memory_bars"]==0)
    best=qualified[0] if qualified else None
    n=sum(start<=b[0]<endms for b in bars)
    gap_segments=sum(bad)
    return {"days":days,"direction": "LONG_ONLY" if allowed==1 else "SHORT_ONLY","source_bars":n,"expected_bars":days*288,"gap_segments":gap_segments,
            "period_start_utc":dt.datetime.fromtimestamp(start/1000,UTC).isoformat(),
            "train_test_boundary_utc":dt.datetime.fromtimestamp(split/1000,UTC).isoformat(),
            "configurations":len(combos),"qualified_train":len(qualified),
            "baseline":baseline,"winner_selected_train_only":best,
            "top10_train":qualified[:10],
            "posthoc_positive_test_variants":sum(c["test"]["net_usdt"]>0 and c["test"]["trades"]>=5 for c in combos),
            "all_variants":combos}

def main():
    start=time.monotonic()
    rows,end,errors=source_data()
    print("BARS",len(rows),"FAIL_ARCHIVES",len(errors),flush=True)
    tests=[]
    for direction in (1,-1):
     for days in (30,60,90):
        x=backtest(rows,end,days,direction)
        tests.append(x)
        print("DIRECTION_WINDOW_RESULT="+json.dumps({k:v for k,v in x.items() if k!="all_variants"},ensure_ascii=False,separators=(",",":")),flush=True)
    obj={"source":"data.binance.vision, Binance SPOT BATUSDT 5m unmodified OHLCV",
         "last_complete_day_end_utc":end.isoformat(),"source_archive_failures":errors,
         "assumptions":{"trade_size_usdt":SIZE,"take_profit_pct":1.0,"stop_loss_pct":.25,
                        "fee_per_side_pct":.06,"time_limit_minutes":10,
                        "entry":"next 5m open after confirmed regime flip",
                        "uncertain_within_bar":"SL first if both touched",
                        "validation":"winner chosen solely from chronological first 70%",
                        "limits":"5m OHLC cannot resolve exact TP/SL ordering or fill spreads; no out-of-sample optimization"},
         "results":tests,"elapsed_seconds":round(time.monotonic()-start,1)}
    p=Path("research/bat_volume_direction_result.json")
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("RESEARCH_REPORT_SAVED",str(p),flush=True)

if __name__=="__main__":
    main()
