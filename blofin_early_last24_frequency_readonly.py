"""Read-only current EARLY setup frequency on a completed UK 12:00-18:00 session.
Exactly the 7/8-day candidate: TOP200 rolling 24h price change, 5m
candle, UK-midnight volume reset, regime change <=3 candles, candle same
color, volume > prior candle and >= 4x previous-20 median, body >=0.4%.
Also count actionable entries with only ONE 20-USDT position globally,
TP1.5% SL1% 60min, optional earlier regime reversal.
Uses publicly available BloFin candle API only; NO credentials or LIVE writes.
"""
from __future__ import annotations
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timedelta,timezone,time
from zoneinfo import ZoneInfo
import json,statistics,sys
import blofin_top200_repeat_setup_research as m

FIVE=m.FIVE
UK=ZoneInfo("Europe/London")
now=datetime.now(UK)
day=now.date() if now.hour>=18 else now.date()-timedelta(days=1)
uk_start=datetime.combine(day,time(12,0),tzinfo=UK)
uk_finish=datetime.combine(day,time(18,0),tzinfo=UK)
uk_simend=datetime.combine(day,time(20,0),tzinfo=UK)
start=int(uk_start.astimezone(timezone.utc).timestamp()*1000)
finish=int(uk_finish.astimezone(timezone.utc).timestamp()*1000)
simend=int(uk_simend.astimezone(timezone.utc).timestamp()*1000)
oldest=start-310*FIVE
t0=datetime.now(timezone.utc)
print("WINDOW="+json.dumps({"run_at_uk":now.isoformat(),"utc_24h_start":(t0-timedelta(hours=24)).isoformat(),
          "uk_session_start":uk_start.isoformat(),"uk_session_end":uk_finish.isoformat(),
          "date_uk":str(day),"oldest_utc":datetime.fromtimestamp(oldest/1000,timezone.utc).isoformat()}),flush=True)
if uk_finish.astimezone(timezone.utc)<t0-timedelta(hours=24):
    raise RuntimeError("Most recent completed 12-18 UK session is not in last 24h")
symbols=m.list_swaps()
print("ELIGIBLE="+str(len(symbols)),flush=True)
raw={}
fail={}
m.MIN_CALL_GAP=.10
m.THREADS=10
with ThreadPoolExecutor(max_workers=10) as pool:
    future_map={pool.submit(m.candles,inst,simend,oldest):inst for inst in symbols}
    for n,fut in enumerate(as_completed(future_map),1):
        inst=future_map[fut]
        try:
            candles,_=fut.result()
            if len(candles)>=310:raw[inst]=candles
            else:fail[inst]="insufficient bars: "+str(len(candles))
        except Exception as exc:fail[inst]=str(exc)
        if n%75==0 or n==len(symbols):
            print(f"FETCH_PROGRESS={n}/{len(symbols)} usable={len(raw)} missing={len(fail)}",flush=True)
print("HISTORY_FETCH="+json.dumps({"eligible":len(symbols),"usable":len(raw),"missing_count":len(fail),"failures":fail}),flush=True)
if len(raw)<200:raise RuntimeError("Cannot rank historical TOP200 with fewer than 200 usable instruments")
timestart=oldest-oldest%FIVE
times=list(range(timestart,simend,FIVE))
states={inst:m.volume_states(bars,times) for inst,bars in raw.items()}
events=[]
rank_coverage=[]
missing_coverage=0
for ts in range(start,finish,FIVE):
    prev=ts-288*FIVE
    ranks=[]
    for inst,bars in raw.items():
        x=bars.get(ts)
        y=bars.get(prev)
        if x and y and y[3]>0:
            ranks.append(((x[3]/y[3]-1),inst))
    if len(ranks)<200:
        missing_coverage+=1
        continue
    ranks.sort(reverse=True)
    rank_coverage.append(len(ranks))
    for rank,(_,inst) in enumerate(ranks[:200],1):
        for sig in m.calc_event(inst,ts,raw[inst],states[inst],rank):
            if sig["family"]!="EARLY":continue
            if sig["ratio"]<4 or sig["body"]<.4:continue
            if not(start<=sig["signal_ms"]<finish):continue
            events.append(sig)
events.sort(key=lambda e:(e["signal_ms"],e["inst"]))
print("RAW_EVENTS="+json.dumps({"count":len(events),"long":sum(e["side"]=="LONG" for e in events),
   "short":sum(e["side"]=="SHORT" for e in events),
   "distinct_instruments":len(set(e["inst"] for e in events)),
   "minutes_covered":len(rank_coverage),"minutes_missing":missing_coverage,
   "rank_population_median":statistics.median(rank_coverage) if rank_coverage else None}),flush=True)
busy=0;trades=[];missing_outcomes=[]
for e in events:
    if e["signal_ms"]<busy:continue
    simulation=m.returns(raw[e["inst"]],states[e["inst"]],e,(.015,.01,12),simend)
    if simulation is None:
        missing_outcomes.append({"coin":e["inst"],"at_ms":e["signal_ms"]})
        continue
    pct,exit_time,reason=simulation
    busy=exit_time
    trades.append({
      "symbol":e["inst"],"side":e["side"],"rank":e["rank"],
      "entry_uk":datetime.fromtimestamp(e["signal_ms"]/1000,UK).strftime("%Y-%m-%d %H:%M"),
      "volume_ratio":round(e["ratio"],2),"body_pct":round(e["body"],3),
      "exit_uk":datetime.fromtimestamp(exit_time/1000,UK).strftime("%Y-%m-%d %H:%M"),
      "reason":reason,"net_return_pct":round(pct-m.COST_PCT,4)})
report={
  "generated_uk":now.isoformat(),"session_uk":[uk_start.isoformat(),uk_finish.isoformat()],
  "eligible":len(symbols),"usable_candles":len(raw),"unavailable_symbols":fail,
  "rank_snapshots":len(rank_coverage),"missing_rank_snapshots":missing_coverage,
  "raw_early_signals":len(events),"raw_long":sum(e["side"]=="LONG" for e in events),
  "raw_short":sum(e["side"]=="SHORT" for e in events),
  "raw_unique_coins":len(set(e["inst"] for e in events)),
  "one_position_trades":len(trades),
  "one_position_wins_net":sum(t["net_return_pct"]>0 for t in trades),
  "one_position_result_net_usdt":round(sum(t["net_return_pct"] for t in trades)*.2,4),
  "skipped_outcomes":missing_outcomes,"trades":trades,
  "sample_of_all_signals":[{"symbol":e["inst"],"side":e["side"],"rank":e["rank"],
                            "signal_uk":datetime.fromtimestamp(e["signal_ms"]/1000,UK).strftime("%Y-%m-%d %H:%M")}
                           for e in events[:40]],
  "notes":"Exact historical TOP200 at each 5m candle from currently listed coins, UK midnight regime reset. Estimated fees+slip 0.22%, no funding or spread variability. One global position; equal-time selection alphabetical. API missing coins weaken precision if any."
}
from pathlib import Path
Path("blofin_early_last24_frequency_report.json").write_text(json.dumps(report,indent=2))
print("EARLY_LAST24_RESULT="+json.dumps({k:v for k,v in report.items() if k not in ("trades","sample_of_all_signals","unavailable_symbols","skipped_outcomes")},ensure_ascii=False,separators=(",",":")),flush=True)
print("EARLY_TRADES="+json.dumps(trades,ensure_ascii=False,separators=(",",":")),flush=True)
