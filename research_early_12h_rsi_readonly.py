"""READ-ONLY exact EARLY strategy comparison across 12h ending 2026-10-18 02:55 BST.

All 390 public BloFin USDT-crypto linear swaps are ranked each 5m by causal
rolling 24h price change, selects TOP200. EARLY signal up to three candles
after UK-midnight-reset volume-regime flip, signal body >=0.4%, volume
>=4x median last 20 bars AND >prior bar, same-side candle. Enters following
5m open. Compare unchanged strategy versus RSI filter (LONG RSI>70, SHORT
RSI<30 blocked), both Wilder (standard) and SMA RSI (prior diagnostic).
Exits TP1.5% SL1% maximum 60m, volume reversal and daily UK midnight.
Fee and slippage 0.22% roundtrip; single globally held trade at a time.
At cutoff, remaining open trades are marked-to-market separately.
No API keys, order submission or edits to trading logic.
"""
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip, json, statistics, time as clock, sys
import blofin_top200_repeat_setup_research as m

FIVE=m.FIVE
UK=ZoneInfo("Europe/London")
END=int(datetime(2026,10,18,1,55,tzinfo=timezone.utc).timestamp()*1000) # UK 02:55
START=END-12*60*60*1000 # UK 2026-10-17 14:55
N=4
m.MIN_CALL_GAP=.18
def formatted(ts):
    return datetime.fromtimestamp(ts/1000,UK).strftime("%Y-%m-%d %H:%M")
def fetch(shard):
    instruments=m.list_swaps()
    mine=instruments[shard::N]
    data={};fail={}
    print("FRESH_BEGIN="+json.dumps({"shard":shard,"eligible":len(instruments),"mine":len(mine),
         "end_uk":formatted(END),"start_uk":formatted(START)}),flush=True)
    # Only candles after previous cached extract necessary: up to ~2h.
    def one(inst):
        rows=m.get_data("/api/v1/market/candles",{"instId":inst,"bar":"5m",
                        "after":str(END+FIVE),"limit":"100"})
        out={}
        for x in rows:
            if len(x)<9 or str(x[8])!="1":continue
            try:
                t=int(x[0])
                if t>=END or t<END-100*FIVE:continue
                b=tuple(float(x[i]) for i in (1,2,3,4,5))
                if b[0]<=0 or b[3]<=0:continue
                out[t]=b
            except (ValueError,TypeError,IndexError):continue
        if len(out)<26:raise RuntimeError("Fewer than 26 closed 5m candles returned")
        return out
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs={pool.submit(one,inst):inst for inst in mine}
        for n,f in enumerate(as_completed(jobs),1):
            inst=jobs[f]
            try:
                vals=f.result()
                data[inst]={str(t):list(b) for t,b in vals.items()}
            except Exception as ex:fail[inst]=str(ex)
            if n%25==0:print("PROGRESS="+json.dumps({"shard":shard,"done":n,"success":len(data),"failed":len(fail)}),flush=True)
    for attempt in range(3):
        if not fail:break
        clock.sleep(4+attempt*5)
        for inst in list(fail):
            try:
                v=one(inst);data[inst]={str(t):list(b) for t,b in v.items()};del fail[inst]
            except Exception as ex:fail[inst]=str(ex)
        print("RETRY="+json.dumps({"shard":shard,"attempt":attempt+1,"success":len(data),"failed":len(fail)}),flush=True)
    with gzip.open(f"rsi12_shard_{shard}.json.gz","wt",encoding="utf-8",compresslevel=3) as f:
        json.dump({"shard":shard,"universe":instruments,"end_ms":END,"candles":data,"failures":fail},f,separators=(",",":"))
    print("FRESH_DONE="+json.dumps({"shard":shard,"success":len(data),"missing":fail}),flush=True)

def rsi_series(bars):
    keys=sorted(bars)
    close=[bars[k][3] for k in keys]
    wilder={};simple={}
    if len(keys)<18:return wilder,simple
    gains=[max(0,close[i]-close[i-1]) for i in range(1,len(close))]
    losses=[max(0,close[i-1]-close[i]) for i in range(1,len(close))]
    def value(g,l):
        return 50. if g==0 and l==0 else 100. if l==0 else 100.-100./(1.+g/l)
    for i in range(14,len(keys)):
        simple[keys[i]]=value(sum(gains[i-14:i])/14,sum(losses[i-14:i])/14)
    ag=sum(gains[:14])/14;al=sum(losses[:14])/14
    wilder[keys[14]]=value(ag,al)
    for i in range(15,len(keys)):
        ag=(ag*13+gains[i-1])/14
        al=(al*13+losses[i-1])/14
        wilder[keys[i]]=value(ag,al)
    return wilder,simple

def count():
    # First combine the 390 historical full market candle archives verified in earlier test.
    allbars={};oldfail={};universe=set()
    for shard in range(N):
        p=Path("old_shards")/f"early_hour_{shard}.json.gz"
        with gzip.open(p,"rt",encoding="utf-8") as f:x=json.load(f)
        assert x["shard"]==shard
        universe.update(x["symbols"])
        oldfail.update(x["missing"])
        for inst,rows in x["rows"].items():
            if inst in allbars:raise RuntimeError("Duplicate old market")
            allbars[inst]={int(t):tuple(v) for t,v in rows.items()}
    freshfail={}
    for shard in range(N):
        p=Path("fresh_shards")/f"rsi12_shard_{shard}.json.gz"
        with gzip.open(p,"rt",encoding="utf-8") as f:x=json.load(f)
        assert x["shard"]==shard and x["end_ms"]==END
        if set(x["universe"])!=universe:raise RuntimeError("Different universe in new fetch")
        freshfail.update(x["failures"])
        for inst,rows in x["candles"].items():
            if inst not in allbars:raise RuntimeError("Coin missing prior OHLCV")
            allbars[inst].update({int(t):tuple(v) for t,v in rows.items()})
    if oldfail or freshfail or len(allbars)!=len(universe):
        print("COVERAGE_ERROR="+json.dumps({"oldmissing":oldfail,"newmissing":freshfail,
                                            "available":len(allbars),"eligible":len(universe)}),flush=True)
        raise RuntimeError("Cannot claim complete 390-coin comparison: some markets missing")

    # Make sure there were not timestamp gaps in the recent merged region.
    gaps={}
    for inst,rows in allbars.items():
        missing=[t for t in range(START-5*FIVE,END,FIVE) if t not in rows]
        if missing:gaps[inst]=len(missing)
    if gaps:
        print("GAPS="+json.dumps(gaps),flush=True)
        raise RuntimeError("Incomplete OHLCV histories for 12-hour test")
    print("COVERAGE_OK="+json.dumps({"eligible":len(universe),"successful":len(allbars),
       "start_uk":formatted(START),"end_uk":formatted(END),"complete_candles":144}),flush=True)

    allkeys=range(min(min(bars) for bars in allbars.values()),END,FIVE)
    states={inst:m.volume_states(bars,allkeys) for inst,bars in allbars.items()}
    rsis={inst:rsi_series(bars) for inst,bars in allbars.items()}
    raw=[];total_rank=0
    for t in range(START-FIVE,END-FIVE,FIVE):
        ranks=[]
        for inst,rows in allbars.items():
            c=rows.get(t);prior=rows.get(t-288*FIVE)
            if c and prior and prior[3]>0:
                ranks.append((c[3]/prior[3]-1,inst))
        if len(ranks)<200:raise RuntimeError(f"Fewer than 200 ranks at {formatted(t)}")
        total_rank+=1
        ranks.sort(reverse=True)
        for rank,(_,inst) in enumerate(ranks[:200],1):
            for e in m.calc_event(inst,t,allbars[inst],states[inst],rank):
                if e["family"]!="EARLY" or e["ratio"]<4 or e["body"]<.4:continue
                if not(START<=e["signal_ms"]<END):continue
                w=rsis[inst][0].get(t);simple=rsis[inst][1].get(t)
                if w is None or simple is None:continue
                e["rsi_wilder"]=w
                e["rsi_simple"]=simple
                raw.append(e)
    raw.sort(key=lambda e:(e["signal_ms"],e["inst"]))
    def summary_indicators(e):
        return {"coin":e["inst"],"side":e["side"],"rank":e["rank"],
                "signal_uk":formatted(e["signal_ms"]),"rsi_wilder":round(e["rsi_wilder"],2),
                "rsi_simple":round(e["rsi_simple"],2),
                "volume_multiple":round(e["ratio"],2),"body_pct":round(e["body"],3)}

    # All 12hr signals, whether or not an active position would have prevented entry
    raw_base=[summary_indicators(e) for e in raw]
    overall_raw={"total":len(raw),"long":sum(e["side"]=="LONG" for e in raw),
                 "short":sum(e["side"]=="SHORT" for e in raw)}
    def allow(e,type):
        if type=="BASELINE":return True
        r=e["rsi_wilder"] if type=="WILDER" else e["rsi_simple"]
        return (r<=70 if e["side"]=="LONG" else r>=30)
    def sim(which):
        busy=0;trades=[]
        for e in raw:
            if not allow(e,which) or e["signal_ms"]<busy:continue
            t=e["signal_ms"];coin=e["inst"];side=e["side"]
            # exact m.returns, except if enough future confirmed bars aren't yet
            # present, mark current unrealized PnL at cutoff instead of claiming exit.
            v=m.returns(allbars[coin],states[coin],e,(.015,.01,12),END)
            if v is None:
                entry=allbars[coin].get(t)
                last=allbars[coin].get(END-FIVE)
                if not entry or not last:continue
                signed=1 if side=="LONG" else -1
                provisional_pct=100*signed*(last[3]/entry[0]-1)-m.COST_PCT
                reason="OPEN_AT_CUTOFF"
                actual_end=END
                netpct=provisional_pct
                busy=END
            else:
                gross,actual_end,reason=v
                netpct=gross-m.COST_PCT
                busy=actual_end
            info=summary_indicators(e)
            info.update({"exit_uk":formatted(actual_end),"exit_reason":reason,
                         "profit_net_pct":round(netpct,4),
                         "net_usdt":round(netpct*m.NOTIONAL/100,5),
                         "finalized":reason!="OPEN_AT_CUTOFF"})
            trades.append(info)
        completed=[x for x in trades if x["finalized"]]
        return {"entries":len(trades),"closed":len(completed),
                "wins_closed":sum(x["profit_net_pct"]>0 for x in completed),
                "losses_closed":sum(x["profit_net_pct"]<=0 for x in completed),
                "open":len(trades)-len(completed),
                "closed_net_USDT":round(sum(x["net_usdt"] for x in completed),4),
                "marked_total_net_USDT":round(sum(x["net_usdt"] for x in trades),4),
                "marked_total_net_pct_reference20":round(sum(x["profit_net_pct"] for x in trades),4),
                "trades":trades}
    baseline=sim("BASELINE")
    standard=sim("WILDER")
    simple=sim("SIMPLE")
    # Compare individual baseline accepted positions against RSI threshold,
    # a correct attribution distinct from re-simulating skipped earlier entries.
    def drops(version):
        dropped=[e for e in baseline["trades"] if (e["rsi_wilder"] if version=="WILDER" else e["rsi_simple"]) > 70 and e["side"]=="LONG" or (e["rsi_wilder"] if version=="WILDER" else e["rsi_simple"])<30 and e["side"]=="SHORT"]
        return {"dropped_baseline_trades":len(dropped),
                "profitable_removed":sum(e["finalized"] and e["profit_net_pct"]>0 for e in dropped),
                "losing_removed":sum(e["finalized"] and e["profit_net_pct"]<=0 for e in dropped),
                "unresolved_removed":sum(not e["finalized"] for e in dropped),
                "removed":dropped}
    report={"period_uk":[formatted(START),formatted(END)],
        "methods":"Same EARLY setup TOP200, LONG RSI >70 filtered / SHORT RSI <30 filtered; RSI14 Wilder and RSI14 simple compared.",
        "eligible_swaps":len(universe),"histories_checked":len(allbars),
        "ranking_snapshots":total_rank,"signal_counts":overall_raw,
        "baseline":baseline,"filter_rsi_wilder14":standard,
        "filter_rsi_simple14":simple,
        "impact_on_baseline_trades_wilder":drops("WILDER"),
        "impact_on_baseline_trades_simple":drops("SIMPLE"),
        "all_raw_signals":raw_base,
        "methodology":"Each variant re-simulates nonoverlapping single-position trading chronologically, after estimated commission+slippage 0.22% roundtrip. Last not-yet-closed positions are marked-to-market at end, NOT counted as finalized wins/losses. Regime volume peak reset at midnight UK. Equal-time candidates sorted by coin. No LIVE changes. Historical TOP200 among currently listed 390 swaps; funding, spread variability, gap fill execution and delisted coins not captured. OHLC stop loss evaluated before take-profit if same candle."}
    p=Path("blofin_early_12h_rsi_filter_report.json")
    p.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("RSI12H_SUMMARY="+json.dumps({k:v for k,v in report.items() if k not in ("all_raw_signals","baseline","filter_rsi_wilder14","filter_rsi_simple14","impact_on_baseline_trades_wilder","impact_on_baseline_trades_simple")},ensure_ascii=False,separators=(",",":")),flush=True)
    print("RSI12H_BASELINE="+json.dumps({k:v for k,v in baseline.items() if k!="trades"},separators=(",",":")),flush=True)
    print("RSI12H_WILDER="+json.dumps({k:v for k,v in standard.items() if k!="trades"},separators=(",",":")),flush=True)
    print("RSI12H_SIMPLE="+json.dumps({k:v for k,v in simple.items() if k!="trades"},separators=(",",":")),flush=True)
    print("RSI12H_REMOVED_WILDER="+json.dumps(drops("WILDER"),separators=(",",":")),flush=True)
    print("RSI12H_REMOVED_SIMPLE="+json.dumps(drops("SIMPLE"),separators=(",",":")),flush=True)
    print("RSI12H_TRADES_BASELINE="+json.dumps(baseline["trades"],separators=(",",":")),flush=True)
    print("RSI12H_TRADES_WILDER="+json.dumps(standard["trades"],separators=(",",":")),flush=True)
    print("RSI12H_TRADES_SIMPLE="+json.dumps(simple["trades"],separators=(",",":")),flush=True)
if __name__=="__main__":
    if sys.argv[1]=="fetch":fetch(int(sys.argv[2]))
    elif sys.argv[1]=="analyze":count()
    else:raise SystemExit("Usage: fetch <0-3> | analyze")
