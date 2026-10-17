"""Read-only segmented BloFin EARLY count for 2026-10-17 23:52–2026-10-18 00:52 UK, ignoring trading hours.
Run fetch <shard_id> on 4 separate GitHub runners to avoid public API
throttling; merge real 5m candles and count exactly the historic 7/8-day
EARLY criterion among point-in-time 24h gainers TOP200.
No LIVE trading, private credentials, or trade endpoints.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timedelta,timezone,time
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import Counter
import json,gzip,statistics,sys,time as tm
import blofin_top200_repeat_setup_research as m

UK=ZoneInfo("Europe/London")
END_DT=datetime(2026,10,18,0,52,20,tzinfo=UK)
BEGIN_DT=END_DT-timedelta(hours=1)
DAY=END_DT.date()
FIVE=m.FIVE
START=int(BEGIN_DT.timestamp()*1000)
FINISH=int(END_DT.timestamp()*1000)
FETCH_END=(FINISH//FIVE)*FIVE
FIRST_TS=((START//FIVE)-1)*FIVE
OLDEST=FIRST_TS-310*FIVE
SIMEND=FETCH_END
SHARDS=4
m.MIN_CALL_GAP=.20

def fetch(n):
    symbols=m.list_swaps()
    if len(symbols)<200:raise RuntimeError("Need at least 200 available instruments")
    these=symbols[n::SHARDS]
    raw={};fail={}
    m.THREADS=5
    print("SHARD_BEGIN="+json.dumps({"shard":n,"total_symbols":len(symbols),
        "selected":len(these),"start_uk":BEGIN_DT.isoformat(),"end_uk":END_DT.isoformat()}),flush=True)
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map={pool.submit(m.candles,inst,SIMEND,OLDEST):inst for inst in these}
        for k,f in enumerate(as_completed(future_map),1):
            inst=future_map[f]
            try:
                candles,_=f.result()
                if len(candles)>=310:raw[inst]={str(ts):list(v) for ts,v in candles.items()}
                else:fail[inst]="Insufficient candles: "+str(len(candles))
            except Exception as exc:fail[inst]=str(exc)
            if k%20==0:
                print("SHARD_PROGRESS="+json.dumps({"shard":n,"fetched":k,
                    "usable":len(raw),"failed":len(fail)}),flush=True)
    # Retry intermittent 429 / 5xx failures after separate runner cooloff.
    for attempt in range(3):
        if not fail:break
        tm.sleep((attempt+1)*6)
        for inst in list(fail):
            try:
                candles,_=m.candles(inst,SIMEND,OLDEST)
                if len(candles)>=310:
                    raw[inst]={str(ts):list(v) for ts,v in candles.items()}
                    del fail[inst]
            except Exception as exc:fail[inst]=str(exc)
        print("RETRY_PROGRESS="+json.dumps({"shard":n,"attempt":attempt+1,
             "usable":len(raw),"failed":len(fail)}),flush=True)
    output={"shard":n,"symbols":these,"all_symbol_count":len(symbols),
            "day_uk":str(DAY),"rows":raw,"missing":fail}
    with gzip.open(f"early_shard_{n}.json.gz","wt",encoding="utf-8",compresslevel=3) as f:
        json.dump(output,f,separators=(",",":"))
    print("SHARD_END="+json.dumps({"shard":n,"usable":len(raw),
            "fail":len(fail),"missing":fail}),flush=True)

def analyze():
    allrows={};missing={};seen=[]
    for n in range(SHARDS):
        filename=Path("shards")/f"early_shard_{n}.json.gz"
        with gzip.open(filename,"rt",encoding="utf-8") as f:payload=json.load(f)
        if payload["day_uk"]!=str(DAY) or payload["shard"]!=n:
            raise RuntimeError("Wrong input shard session/date")
        seen.extend(payload["symbols"])
        for name,rows in payload["rows"].items():
            if name in allrows:raise RuntimeError("Duplicated instrument")
            allrows[name]={int(k):tuple(v) for k,v in rows.items()}
        missing.update(payload["missing"])
    print("MERGED_HISTORY="+json.dumps({"eligible":len(set(seen)),
           "usable":len(allrows),"missing":missing}),flush=True)
    if len(allrows)<300:
        raise RuntimeError("Market coverage too low for reliable historical TOP200")
    timekeys=range(OLDEST-OLDEST%FIVE,SIMEND,FIVE)
    regimes={inst:m.volume_states(bars,timekeys) for inst,bars in allrows.items()}
    rank_counts=[];events=[];bad=0
    for ts in range(FIRST_TS,FETCH_END,FIVE):
        price_prev=ts-288*FIVE
        ranks=[]
        for inst,bars in allrows.items():
            this=bars.get(ts);prior=bars.get(price_prev)
            if this and prior and prior[3]>0:
                ranks.append((this[3]/prior[3]-1,inst))
        if len(ranks)<200:
            bad+=1
            continue
        ranks.sort(reverse=True)
        rank_counts.append(len(ranks))
        for rank,(_,inst) in enumerate(ranks[:200],1):
            for e in m.calc_event(inst,ts,allrows[inst],regimes[inst],rank):
                if e["family"]=="EARLY" and e["ratio"]>=4 and e["body"]>=.4 and START<=e["signal_ms"]<FINISH:
                    events.append(e)
    if bad:raise RuntimeError("Incomplete rank snapshots, cannot infer exact count")
    events.sort(key=lambda e:(e["signal_ms"],e["inst"]))
    # Recent signals are counted without exit simulation: later 5m bars and
    # 60-minute outcome are not yet observable. No misleading wins or PnL.
    trades=[{"coin":e["inst"],"side":e["side"],"rank":e["rank"],
        "signal_uk":datetime.fromtimestamp(e["signal_ms"]/1000,UK).strftime("%Y-%m-%d %H:%M"),
        "volume_median20_multiple":round(e["ratio"],2),
        "body_pct":round(e["body"],3)} for e in events]
    data={
       "as_of_day_uk":str(DAY),"time_window_uk":[BEGIN_DT.isoformat(),END_DT.isoformat()],"trading_hours_enforced":False,
       "eligible_crypto_usdt_swaps":len(set(seen)),
       "symbols_with_history":len(allrows),"unavailable":missing,
       "historical_rank_snapshots":len(rank_counts),"median_rank_coverage":statistics.median(rank_counts),
       "raw_signals":len(events),"raw_long":sum(e["side"]=="LONG" for e in events),
       "raw_short":sum(e["side"]=="SHORT" for e in events),
       "raw_unique_coins":len(set(e["inst"] for e in events)),
       "signals_in_session_hours":0,
       "signal_count_outside_session":len(events),
       "outcome_count_not_evaluated":len(events),
       "trades":trades,
       "limitations":"TOP200 reconstructed from 24h price change among currently listed crypto swaps. Session 12:00-18:00 not enforced; this is raw pattern-only signal count. No future exit simulation."
    }
    Path("blofin_early_last_hour_20261018.json").write_text(json.dumps(data,indent=2))
    print("EARLY_HOUR_RESULT="+json.dumps({k:v for k,v in data.items() if k!="trades"},separators=(",",":")),flush=True)
    print("SIGNALS="+json.dumps(trades,separators=(",",":")),flush=True)

if __name__=="__main__":
    if len(sys.argv)<2:raise SystemExit("Usage: fetch 0-3 | analyze")
    if sys.argv[1]=="fetch":fetch(int(sys.argv[2]))
    elif sys.argv[1]=="analyze":analyze()
    else:raise SystemExit("Bad command")
