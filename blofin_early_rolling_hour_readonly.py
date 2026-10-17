"""Public BloFin 5m read-only EARLY signal scan for 60-minute window ending
2026-10-18 00:50 BST. Reconstructed point-in-time TOP200 24h gainers,
UK midnight volume reset, only signal on CLOSED bars. NO LIVE writes."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import gzip,json,statistics,sys,time as tm
from pathlib import Path
import blofin_top200_repeat_setup_research as m
UK=ZoneInfo("Europe/London"); FIVE=m.FIVE
END=int(datetime(2026,10,17,23,50,tzinfo=timezone.utc).timestamp()*1000)
START=END-12*FIVE
OLDEST=START-330*FIVE
SHARDS=4
m.MIN_CALL_GAP=.18

def fetch(shard):
    # lastclose provides a stable OHLCV snapshot through the final signal candle.
    cutoff=END+FIVE
    symbols=m.list_swaps()
    these=symbols[shard::SHARDS]
    raw={}; missing={}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures={executor.submit(m.candles,x,cutoff,OLDEST):x for x in these}
        for i,f in enumerate(as_completed(futures),1):
            x=futures[f]
            try:
                bars,_=f.result()
                if len(bars)>=305:raw[x]={str(k):list(v) for k,v in bars.items()}
                else:missing[x]="Only "+str(len(bars))+" candles"
            except Exception as exc:missing[x]=str(exc)
            if i%25==0:print("PROGRESS="+json.dumps({"shard":shard,"done":i,"good":len(raw),"bad":len(missing)}),flush=True)
    # If rate limited, retry for higher coverage.
    for i in range(3):
        if not missing:break
        tm.sleep(3+i*4)
        for x in list(missing):
            try:
                bars,_=m.candles(x,cutoff,OLDEST)
                if len(bars)>=305:
                    raw[x]={str(k):list(v) for k,v in bars.items()}
                    del missing[x]
            except Exception as exc:missing[x]=str(exc)
        print("RETRY="+json.dumps({"shard":shard,"attempt":i+1,"good":len(raw),"bad":len(missing)}),flush=True)
    with gzip.open("early_hour_"+str(shard)+".json.gz","wt",encoding="utf-8",compresslevel=3) as f:
        json.dump({"shard":shard,"anchor":END,"symbols":these,"rows":raw,"missing":missing},f,separators=(",",":"))
    print("FETCH_DONE="+json.dumps({"shard":shard,"eligible":len(these),"good":len(raw),"missing":missing}),flush=True)

def analyze():
    allrows={}; missing={};eligible=set()
    for shard in range(SHARDS):
        with gzip.open(Path("shards")/("early_hour_"+str(shard)+".json.gz"),"rt") as f:d=json.load(f)
        assert d["shard"]==shard and d["anchor"]==END
        eligible.update(d["symbols"])
        missing.update(d["missing"])
        for x,rows in d["rows"].items():
            if x in allrows:raise RuntimeError("Duplicate "+x)
            allrows[x]={int(k):tuple(v) for k,v in rows.items()}
    print("COVERAGE="+json.dumps({"eligible":len(eligible),"available":len(allrows),"unavailable":missing}),flush=True)
    if len(allrows)<300:raise RuntimeError("Missing too many instruments for TOP200 ranking")
    alltimes=range(OLDEST-OLDEST%FIVE,END+FIVE,FIVE)
    states={x:m.volume_states(b,alltimes) for x,b in allrows.items()}
    count_per_rank=[]; signals=[]; gaps=[]
    for ts in range(START,END,FIVE):
        ranked=[]
        for x,bars in allrows.items():
            cur=bars.get(ts);prev=bars.get(ts-288*FIVE)
            if cur is not None and prev is not None and prev[3]>0:
                ranked.append((cur[3]/prev[3]-1,x))
        if len(ranked)<200:
            gaps.append(ts);continue
        ranked.sort(reverse=True)
        count_per_rank.append(len(ranked))
        for rank,(_,x) in enumerate(ranked[:200],1):
            for sig in m.calc_event(x,ts,allrows[x],states[x],rank):
                if sig["family"]!="EARLY" or sig["ratio"]<4 or sig["body"]<0.4:continue
                if START<sig["signal_ms"]<=END:
                    signals.append(sig)
    if gaps:raise RuntimeError("Not all 12 five-minute ranks available")
    signals.sort(key=lambda e:(e["signal_ms"],e["inst"]))
    def clock(t):
        return datetime.fromtimestamp(t/1000,UK).strftime("%Y-%m-%d %H:%M")
    displayed=[{"coin":s["inst"],"side":s["side"],"rank":s["rank"],
                "at_uk":clock(s["signal_ms"]),
                "volume_ratio":round(s["ratio"],2),"body_pct":round(s["body"],2)} for s in signals]
    # For realized outcomes, do not use future after END. Here estimate how many
    # would enter if each trade holds for up to an hour with early TP/SL exits.
    # Count open positions distinctly, rather than counting them as gains.
    busy=0;positions=[]
    for e in signals:
        if e["signal_ms"]<busy:continue
        side=1 if e["side"]=="LONG" else -1
        entry_ts=e["signal_ms"]
        price=allrows[e["inst"]].get(entry_ts)
        if price is None:continue
        entry=price[0];close_ms=min(entry_ts+12*FIVE,END)
        exitrec=None
        for t in range(entry_ts,close_ms,FIVE):
            bar=allrows[e["inst"]].get(t)
            if not bar:break
            tp=(bar[1]>=entry*1.015 if side==1 else bar[2]<=entry*.985)
            sl=(bar[2]<=entry*.99 if side==1 else bar[1]>=entry*1.01)
            if sl or tp:
                exitrec={"exit_uk":clock(t+FIVE),"reason":"SL" if sl else "TP","net_pct":-1.22 if sl else 1.28}
                busy=t+FIVE
                break
        if exitrec is None:
            # If position extends beyond the observation cutoff, subsequent
            # signals cannot be treated as new entries for a serial bot.
            if entry_ts+12*FIVE>END:
                busy=entry_ts+12*FIVE
                exitrec={"reason":"OPEN_OR_UNOBSERVED","exit_uk":None,"net_pct":None}
            else:
                last=allrows[e["inst"]].get(entry_ts+11*FIVE)
                if last:
                    pct=100*side*(last[3]/entry-1)-m.COST_PCT
                    exitrec={"reason":"TIME","exit_uk":clock(entry_ts+12*FIVE),"net_pct":round(pct,4)}
                    busy=entry_ts+12*FIVE
                else:
                    busy=entry_ts+12*FIVE
                    exitrec={"reason":"MISSING_OUTCOME","exit_uk":None,"net_pct":None}
        positions.append({"coin":e["inst"],"side":e["side"],"entry_uk":clock(entry_ts),**exitrec})
    report={"period_uk":[clock(START),clock(END)],"candle_interval_min":5,
            "eligible":len(eligible),"fetched":len(allrows),"missing":missing,
            "historical_rank_snapshots":len(count_per_rank),
            "signals":len(signals),"long":sum(e["side"]=="LONG" for e in signals),
            "short":sum(e["side"]=="SHORT" for e in signals),
            "possible_serial_entries":len(positions),"entries":positions,
            "all_signals":displayed,
            "rules":"EARLY: first 0-3 bars after reversal of peak volume regime, same-direction candle, volume exceeds previous bar and is >=4x median of previous20, candle body >=0.4%; point-in-time TOP200 historical 24h price gainers; no hour-of-day restriction requested for rolling hour.",
            "caution":"2026-10-18 00:50 BST cutoff fixed at user request time. Missing symbols if any make ranked shortlist approximate. Unclosed entries cannot be assigned win/loss. No trading writes."}
    Path("blofin_early_rolling_hour_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print("EARLY_HOUR_RESULT="+json.dumps({k:v for k,v in report.items() if k not in ("entries","all_signals","missing")},separators=(",",":")),flush=True)
    print("EARLY_HOUR_SIGNALS="+json.dumps(displayed,separators=(",",":")),flush=True)
    print("EARLY_HOUR_POSITIONS="+json.dumps(positions,separators=(",",":")),flush=True)

if __name__=="__main__":
    if sys.argv[1]=="fetch":fetch(int(sys.argv[2]))
    elif sys.argv[1]=="analyze":analyze()
    else:raise SystemExit("Usage: fetch n|analyze")
