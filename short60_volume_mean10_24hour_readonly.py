"""READ ONLY: 24 hourly validation samples of SHORT volume reversal with +60% vs prior bar.
FROZEN TEST: 2026-10-17 18:00 to 2026-10-18 18:00 UK/BST.
Compare the existing mean10 suppression rule with and without 60% entry filter.
Fetches *public* closed BloFin 5m OHLCV only; no accounts or LIVE writes.
"""
from __future__ import annotations
import collections
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import statistics
import sys
import time
from zoneinfo import ZoneInfo
import blofin_top200_repeat_setup_research as api

FIVE=300_000
UK=ZoneInfo("Europe/London")
START=int(datetime(2026,10,17,17,0,tzinfo=timezone.utc).timestamp()*1000)
END=int(datetime(2026,10,18,17,0,tzinfo=timezone.utc).timestamp()*1000)
OLDEST=START-300*FIVE
LAST_CLOSE=END+FIVE
SHARDS=4
api.MIN_CALL_GAP=0.7

def clock(ms):
    return datetime.fromtimestamp(ms/1000,UK).strftime("%Y-%m-%d %H:%M")

def side(b):
    if b[3]>b[0]:return "LONG"
    if b[3]<b[0]:return "SHORT"
    return None

def fetch(shard):
    syms=api.list_swaps()
    if len(syms)<200:
        raise RuntimeError("BloFin returned fewer than 200 eligible crypto USDT futures")
    these=syms[shard::SHARDS]
    accepted={}
    errors={}
    def fetch_one(inst):
        rows,pages=api.candles(inst,LAST_CLOSE,OLDEST)
        if rows.get(START-288*FIVE) is None or rows.get(END-FIVE) is None:
            raise ValueError("Missing 24h warmup or last signal bar")
        return rows
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(fetch_one,x):x for x in these}
        for i,f in enumerate(as_completed(futures),1):
            x=futures[f]
            try:
                accepted[x]=f.result()
            except Exception as e:
                errors[x]=str(e)[:130]
            if i%20==0:
                print("SHARD_PROGRESS="+json.dumps({"shard":shard,"done":i,
                        "total":len(these),"ok":len(accepted),"failed":len(errors)}),flush=True)
    # Retry missing symbols under stricter global request pacing.
    api.MIN_CALL_GAP=1.0
    for x in list(errors):
        try:
            accepted[x]=fetch_one(x)
            del errors[x]
        except Exception as e:
            errors[x]=str(e)[:130]
    with gzip.open(f"short60_24h_shard_{shard}.json.gz","wt",encoding="utf-8",compresslevel=3) as fh:
        json.dump({"shard":shard,"asof_end":END,"total":len(syms),
                   "symbols":these,"data":accepted,"errors":errors},fh,separators=(",",":"))
    print("SHARD_DONE="+json.dumps({"shard":shard,"requested":len(these),
                   "good":len(accepted),"failed":len(errors),"errors":errors}),flush=True)

def load():
    data={}
    errors={}
    syms=set()
    totals=set()
    for shard in range(SHARDS):
        name=Path("shards")/f"short60_24h_shard_{shard}.json.gz"
        with gzip.open(name,"rt",encoding="utf-8") as f: bundle=json.load(f)
        if bundle["shard"]!=shard or bundle["asof_end"]!=END:
            raise RuntimeError("Bad shard anchor")
        totals.add(bundle["total"])
        errors.update(bundle["errors"])
        syms.update(bundle["symbols"])
        for x,rows in bundle["data"].items():
            if x in data:raise RuntimeError("Duplicate coin: "+x)
            data[x]={int(t):tuple(v) for t,v in rows.items()}
    if len(totals)!=1 or len(syms)!=next(iter(totals)):
        raise RuntimeError("Four shard instrument snapshots disagree")
    if len(data)<300:
        raise RuntimeError(f"Only {len(data)} symbols have full candles, cannot reliably rank TOP200")
    print("DATA_READY="+json.dumps({"total":len(syms),"good":len(data),"missing":len(errors)}),flush=True)
    return data,errors,len(syms)

def rankings(data):
    ranks={}
    coverage=[]
    for ts in range(START-FIVE,END,FIVE):
        prices=[]
        previous=ts-288*FIVE
        for inst,rows in data.items():
            current=rows.get(ts); before=rows.get(previous)
            if current is not None and before is not None and before[3]>0:
                prices.append((current[3]/before[3]-1,inst))
        if len(prices)<200:
            raise RuntimeError(f"Not enough historical 24-hour return snapshots at {clock(ts)}: {len(prices)}")
        prices.sort(reverse=True)
        ranks[ts]={inst:i for i,(_,inst) in enumerate(prices[:200],1)}
        coverage.append(len(prices))
    return ranks,coverage

def test_period(data,ranks,start,end,threshold):
    """One position PER COIN, matching the two previous independent-hour tests.
    Never add an outcome after the period end. All signals confirmed on closed bar.
    Exclude every bar with vol > mean(previous 10), for both entries AND exits.
    """
    closed=[]
    opened=[]
    candidates=blocked_avg=blocked_60=0
    for inst,rows in data.items():
        pos=None
        for ts in range(start-FIVE,end,FIVE):
            b=rows.get(ts)
            if b is None: continue
            cur=side(b)
            ten=[rows.get(ts-j*FIVE) for j in range(1,11)]
            if any(z is None for z in ten):continue
            avg=statistics.fmean(z[4] for z in ten)
            if avg<=0:continue
            vol=b[4]
            allowed=vol<=avg
            if pos is not None and ts>=pos["entry_ms"] and cur is not None and allowed:
                if cur==pos["side"]:
                    pos["last_same_volume"]=vol
                elif vol>pos["last_same_volume"]:
                    future=rows.get(ts+FIVE)
                    if future is not None and ts+FIVE<=end:
                        move=100*(future[0]/pos["price"]-1)*(1 if pos["side"]=="LONG" else -1)
                        closed.append({"inst":inst,"side":pos["side"],
                           "rank":pos["rank"],"entry_ms":pos["entry_ms"],"exit_ms":ts+FIVE,
                           "volume_ratio":pos["vol_ratio"],"volume_mean10_ratio":pos["vol_mean10_ratio"],
                           "gross_pct":round(move,6),
                           "outcome":"WIN" if move>0 else "LOSS" if move<0 else "FLAT"})
                        pos=None
            old=rows.get(ts-FIVE)
            signal=(cur=="SHORT" and old is not None and side(old)=="LONG"
                    and vol>old[4] and start<=ts+FIVE<end and inst in ranks[ts])
            if signal:
                candidates+=1
                if not allowed:
                    blocked_avg+=1
                elif vol<old[4]*threshold:
                    blocked_60+=1
            if pos is None and signal and allowed and vol>=old[4]*threshold:
                future=rows.get(ts+FIVE)
                if future is not None and future[0]>0:
                    pos={"side":cur,"price":future[0],"entry_ms":ts+FIVE,
                         "last_same_volume":vol,"rank":ranks[ts][inst],
                         "vol_ratio":round(vol/old[4],5) if old[4]>0 else None,
                         "vol_mean10_ratio":round(vol/avg,5)}
        if pos is not None:
            opened.append({"inst":inst,"side":pos["side"],"entry_ms":pos["entry_ms"]})
    counts=collections.Counter(t["outcome"] for t in closed)
    wins=counts["WIN"]; losses=counts["LOSS"]
    gross=sum(t["gross_pct"] for t in closed)
    fee=0.12*len(closed)
    return {"candidates":candidates,"rejected_above_mean10":blocked_avg,
            "rejected_below_60pct_increase":blocked_60,
            "closed":len(closed),"wins":wins,"losses":losses,"flat":counts["FLAT"],
            "still_open":len(opened),
            "win_rate_gross_pct":round(100*wins/(wins+losses),2) if wins+losses else None,
            "gross_pnl_sum_percentage_points":round(gross,5),
            "estimated_net_sum_percentage_points":round(gross-fee,5),
            "estimated_net_usdt_20_per_entry":round(20*(gross-fee)/100,5),
            "trades":closed}

def summarize(rows):
    wins=sum(x["wins"] for x in rows)
    losses=sum(x["losses"] for x in rows)
    flats=sum(x["flat"] for x in rows)
    n=wins+losses
    return {"hours":len(rows),
            "closed":sum(x["closed"] for x in rows),
            "wins":wins,"losses":losses,"flat":flats,
            "still_open_at_hour_end":sum(x["still_open"] for x in rows),
            "winrate_pct":round(100*wins/n,2) if n else None,
            "gross_sum_percentage_points":round(sum(x["gross_pnl_sum_percentage_points"] for x in rows),5),
            "estimated_net_usdt_20_per_entry":round(sum(x["estimated_net_usdt_20_per_entry"] for x in rows),5),
            "hours_more_wins_than_losses":sum(x["wins"]>x["losses"] for x in rows),
            "hours_profitable_after_estimated_fees":sum(x["estimated_net_usdt_20_per_entry"]>0 for x in rows),
            "total_signals":sum(x["candidates"] for x in rows),
            "excluded_above_mean10":sum(x["rejected_above_mean10"] for x in rows)}

def analyse():
    data,errors,total=load()
    ranks,coverage=rankings(data)
    hourly={}
    for threshold,name in ((1.0,"short_without_60pct"),(1.6,"short_min_60pct")):
        rows=[]
        for i in range(24):
            a=START+i*60*60*1000
            b=a+60*60*1000
            result=test_period(data,ranks,a,b,threshold)
            hour={k:v for k,v in result.items() if k!="trades"}
            hour["start_uk"]=clock(a)
            hour["end_uk"]=clock(b)
            rows.append(hour)
            print("HOUR_RESULT="+json.dumps({"variant":name,**hour},separators=(",",":")),flush=True)
        hourly[name]={"total":summarize(rows),"by_hour":rows}
        print("TOTAL_24H="+json.dumps({"variant":name,**hourly[name]["total"]},separators=(",",":")),flush=True)
    # Independently validate previous exactly-one-hour 15-16 SHORT baseline 35w31l and +60 29w19l.
    i=21 # day start 17th 18:00 to 18th 15:00
    assert hourly["short_without_60pct"]["by_hour"][i]["start_uk"]=="2026-10-18 15:00"
    observed=hourly["short_without_60pct"]["by_hour"][i]
    test60=hourly["short_min_60pct"]["by_hour"][i]
    same_as_previous=(observed["wins"]==35 and observed["losses"]==31
                      and test60["wins"]==29 and test60["losses"]==19)
    print("PREVIOUS_HOUR_REPRODUCED="+str(same_as_previous),flush=True)
    # Continuous day: avoid arbitrary end-hour censoring. Last incomplete opens excluded.
    continuous={}
    for threshold,name in ((1.0,"short_without_60pct"),(1.6,"short_min_60pct")):
        allres=test_period(data,ranks,START,END,threshold)
        continuous[name]={k:v for k,v in allres.items() if k!="trades"}
        print("CONTINUOUS_24H="+json.dumps({"variant":name,**continuous[name]},separators=(",",":")),flush=True)
    report={"period_uk":[clock(START),clock(END)],
            "symbol_universe":"Currently trading BloFin crypto USDT linear swaps, ranking historical TOP200 by 24h close-price gain each five minutes using only prior or signal bar.",
            "asof_complete_5m":True,
            "source_symbols":total,"symbols_with_full_data":len(data),
            "missing":errors,
            "rank_count_min":min(coverage),
            "method":"SHORT after red signal candle following green bar; signal volume>previous candle, plus threshold 1.6x variant. Ignore all candles with vol>arithmetic mean previous 10 for entries/exits. Entry next bar open; close next bar open when opposite green eligible candle volume>last eligible red volume. 5m bars. Only completed positions counted. Per-coin independent positions.",
            "costs":"0.12% of notional estimated fees roundtrip for net. Excludes spread/slippage/funding. No TP or SL except eligible volume reversal.",
            "hourly_independent_reset":hourly,
            "continuous_24h_across_hour_boundaries":continuous,
            "previous_hour_15_to_16_reproduction":same_as_previous,
            "limitations":"Hour-boundary censoring differs from 24h continuous; opens excluded rather than losses. Independent positions on different coins may overlap, not executable simultaneously with single 20USDT portfolio. No strategy parameter selection made on test day; no LIVE changes."}
    file=Path("short60_volume_mean10_24hour_report_20261018.json")
    file.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("RESULT_FILE="+str(file),flush=True)

if __name__=="__main__":
    if len(sys.argv)==3 and sys.argv[1]=="fetch":
        fetch(int(sys.argv[2]))
    elif len(sys.argv)==2 and sys.argv[1]=="analyse":
        analyse()
    else:raise SystemExit("Usage: fetch SHARD | analyse")
