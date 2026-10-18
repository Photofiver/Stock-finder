"""Read-only frozen 2026-10-18 16:00-17:00 BST comparison of user's volume rule.
No API keys; no orders; does not touch LIVE state.
"""
from __future__ import annotations
import collections
import concurrent.futures
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import blofin_top200_repeat_setup_research as source

STEP=300000
START=int(datetime(2026,10,18,15,0,tzinfo=timezone.utc).timestamp()*1000)
END=int(datetime(2026,10,18,16,0,tzinfo=timezone.utc).timestamp()*1000)
CURSOR=END+STEP
EARLIEST=START-300*STEP
VARIANTS=("NO_FILTER","SKIP_ABOVE_MEAN10_ENTRY","SKIP_ABOVE_MEAN10_ALL","ONLY_ABOVE_MEAN10_ENTRY")

def color(b):
    if b[3]>b[0]:return "LONG"
    if b[3]<b[0]:return "SHORT"
    return None

def fetch_symbol(inst):
    data,pages=source.candles(inst,CURSOR,EARLIEST)
    return (inst,data,pages)

def load():
    syms=source.list_swaps()
    print("LIVE_CRYPTO_USDT_SYMBOLS="+str(len(syms)),flush=True)
    data={}
    failed={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        fut={pool.submit(fetch_symbol,x):x for x in syms}
        for n,job in enumerate(concurrent.futures.as_completed(fut),1):
            inst=fut[job]
            try:
                _,rows,pages=job.result()
                if len(rows)>=290:
                    data[inst]=rows
                else:
                    failed[inst]="insufficient "+str(len(rows))
            except Exception as e:
                failed[inst]=str(e)[:140]
            if n%70==0:
                print("FETCH_PROGRESS="+str(n)+"/"+str(len(syms))+" OK="+str(len(data))+" FAILED="+str(len(failed)),flush=True)
    print("FETCH_COMPLETE="+str(len(data))+" FAILED="+str(len(failed)),flush=True)
    if len(data)<200:
        raise RuntimeError("Cannot reconstruct historical TOP200 with fewer than 200 symbols")
    return data,failed,len(syms)

def analyse(data,failed,total):
    rank_at={}
    snapshots=[]
    for ts in range(START-STEP,END,STEP):
        vals=[]
        for inst,rows in data.items():
            c=rows.get(ts)
            prev=rows.get(ts-288*STEP)
            if c is not None and prev is not None and prev[3]>0:
                vals.append((c[3]/prev[3]-1,inst))
        if len(vals)<200:
            raise RuntimeError("Incomplete ranking at "+str(ts)+" eligible="+str(len(vals)))
        vals.sort(reverse=True)
        rank_at[ts]={x:i for i,(_,x) in enumerate(vals[:200],1)}
        snapshots.append(len(vals))
    results={}
    for variant in VARIANTS:
        trades=[]
        seen=0
        skipped=0
        rejected_missing=0
        for inst,rows in data.items():
            pos=None
            for ts in range(START-STEP,END,STEP):
                b=rows.get(ts)
                if not b:
                    continue
                side=color(b)
                ten=[rows.get(ts-j*STEP) for j in range(1,11)]
                if any(v is None for v in ten): 
                    rejected_missing+=1
                    continue
                avg=statistics.fmean(x[4] for x in ten)
                if avg<=0:continue
                volume=b[4]
                high=volume>avg
                accepted=(variant=="NO_FILTER" or
                          variant in ("SKIP_ABOVE_MEAN10_ENTRY","SKIP_ABOVE_MEAN10_ALL") and not high or
                          variant=="ONLY_ABOVE_MEAN10_ENTRY" and high)
                ignored_bar=(variant=="SKIP_ABOVE_MEAN10_ALL" and high)
                # If the opposite-colour current bar is bigger than the latest
                # same-colour bar, close at NEXT bar open, not retrospectively.
                if pos is not None and ts>=pos["entry_ms"] and side is not None and not ignored_bar:
                    if side==pos["side"]:
                        pos["last_same_volume"]=volume
                    elif volume>pos["last_same_volume"]:
                        future=rows.get(ts+STEP)
                        if future is not None and ts+STEP<=END:
                            exit_price=future[0]
                            sign=1 if pos["side"]=="LONG" else -1
                            pct=100*sign*(exit_price/pos["entry_price"]-1)
                            trades.append({"coin":inst,"side":pos["side"],
                              "rank":pos["rank"],"entry_ms":pos["entry_ms"],
                              "exit_ms":ts+STEP,"entry_price":pos["entry_price"],
                              "exit_price":exit_price,"gross_pct":round(pct,5),
                              "exit_volume":volume,"last_same_volume":pos["last_same_volume"],
                              "reason":"OPPOSITE_VOLUME"})
                            pos=None
                old=rows.get(ts-STEP)
                signal=(side is not None and old is not None and color(old) is not None
                        and color(old)!=side and volume>old[4]
                        and START<=ts+STEP<END and inst in rank_at[ts])
                if signal:
                    seen+=1
                    if not accepted:skipped+=1
                if pos is None and signal and accepted:
                    nxt=rows.get(ts+STEP)
                    if nxt and nxt[0]>0:
                        pos={"coin":inst,"side":side,"rank":rank_at[ts][inst],
                             "entry_ms":ts+STEP,"entry_price":nxt[0],
                             "last_same_volume":volume}
            if pos is not None:
                # Open at period end; floating P&L cannot count as a win or loss.
                last=rows.get(END-STEP)
                if last:
                    sign=1 if pos["side"]=="LONG" else -1
                    pos["floating_pct"]=round(100*sign*(last[3]/pos["entry_price"]-1),5)
                else:pos["floating_pct"]=None
                trades.append({**pos,"reason":"OPEN_AT_END"})
        closed=[t for t in trades if t["reason"]=="OPPOSITE_VOLUME"]
        open_=[t for t in trades if t["reason"]=="OPEN_AT_END"]
        wins=sum(t["gross_pct"]>0 for t in closed)
        losses=sum(t["gross_pct"]<0 for t in closed)
        breakeven=len(closed)-wins-losses
        rated=wins+losses
        results[variant]={
          "candidate_color_volume_pairs":seen,
          "signals_rejected_by_mean10":skipped,
          "closed":len(closed),"wins":wins,"losses":losses,
          "breakeven":breakeven,
          "winrate_pct_excluding_zero":round(100*wins/rated,2) if rated else None,
          "winrate_pct_all_closed":round(100*wins/len(closed),2) if closed else None,
          "gross_sum_pct_points":round(sum(t["gross_pct"] for t in closed),4),
          "still_open":len(open_),
          "sample_trades":closed[:12],
          "open_samples":[{k:v for k,v in t.items() if k!="last_same_volume"} for t in open_[:8]]
        }
        print("VOLUME_HOUR_RESULT="+json.dumps({"variant":variant,
          **{k:v for k,v in results[variant].items() if k not in ("sample_trades","open_samples")}},
          separators=(",",":"),ensure_ascii=False),flush=True)
    report={"period_uk":"2026-10-18 16:00-17:00 BST",
      "candles":"BloFin closed 5m candles; entry next candle open; exit next candle open on first opposing colour volume strictly greater than latest same-colour volume.",
      "coin_universe":"TOP200 reconstructed every 5m from 24h change at signal, among currently trading crypto USDT swaps.",
      "mean10":"Arithmetic average of preceding 10 volume bars, excluding current signal bar. Compare no filter vs excluding signal bars above average; also exclude large bars at exits in ALL variant.",
      "roundtrip_fee_assumed_pct":0.0,
      "symbols_current":total,
      "symbols_with_history":len(data),
      "failed_symbols":failed,
      "rank_snapshot_count":len(snapshots),
      "rank_available_min":min(snapshots),
      "outcomes":results,
      "limitations":"Independent per-coin backtest, simultaneous positions possible; no price TP/SL. Only actually closed trades counted in wins/losses, open trades reported. No fees, slippage or spread. Currently listed symbols only; unavailable coins excluded; no LIVE changes."}
    target=Path("volume_rule_mean10_hour_20261018_report.json")
    target.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print("RESULT_REPORT="+str(target),flush=True)

if __name__=="__main__":
    d,failed,total=load()
    analyse(d,failed,total)
