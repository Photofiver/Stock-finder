"""READ ONLY: detail how winning vs losing volume reversals differed.
Reuses existing 2026-10-18 16:00-17:00 UK public BloFin shards; no API calls.
"""
from __future__ import annotations
import gzip
import json
import statistics
from collections import Counter
from pathlib import Path
import volume_rule_mean10_hour_20261018_readonly as baseline

FIVE=baseline.STEP
START=baseline.START
END=baseline.END

def median(arr):
    return round(statistics.median(arr),4) if arr else None

def mean(arr):
    return round(statistics.fmean(arr),4) if arr else None

def load():
    out={}
    for n in range(4):
        with gzip.open(Path("shards")/f"mean10_shard_{n}.json.gz","rt") as f:
            shard=json.load(f)
        for inst,rows in shard["data"].items():
            if inst in out:raise RuntimeError("Duplicate instrument: "+inst)
            out[inst]={int(k):tuple(v) for k,v in rows.items()}
    if len(out)!=390:raise RuntimeError(f"Incomplete source {len(out)}/390")
    return out

def run():
    data=load()
    ranking={}
    for ts in range(START-FIVE,END,FIVE):
        eligible=[]
        for inst,rows in data.items():
            b=rows.get(ts)
            older=rows.get(ts-288*FIVE)
            if b is not None and older is not None and older[3]>0:
                eligible.append((b[3]/older[3]-1,inst))
        eligible.sort(reverse=True)
        ranking[ts]={inst:i for i,(_,inst) in enumerate(eligible[:200],1)}
        if len(eligible)<200:raise RuntimeError("Less than 200 on "+str(ts))
    alltrades=[]
    for inst,rows in data.items():
        pos=None
        for ts in range(START-FIVE,END,FIVE):
            b=rows.get(ts)
            if b is None:continue
            side=baseline.color(b)
            prev10=[rows.get(ts-j*FIVE) for j in range(1,11)]
            if any(v is None for v in prev10):continue
            avg10=statistics.fmean(x[4] for x in prev10)
            if avg10<=0:continue
            vol=b[4]
            if pos is not None and ts>=pos["entry_ms"] and side is not None:
                if side==pos["side"]:
                    pos["last_same_volume"]=vol
                elif vol>pos["last_same_volume"]:
                    nextbar=rows.get(ts+FIVE)
                    if nextbar is not None and ts+FIVE<=END:
                        price=nextbar[0]
                        sign=1 if pos["side"]=="LONG" else -1
                        pnl=100*sign*(price/pos["entry_price"]-1)
                        alltrades.append({"coin":inst,"direction":pos["side"],
                          "rank":pos["rank"],"entry_ms":pos["entry_ms"],
                          "exit_ms":ts+FIVE,
                          "gross_pct":round(pnl,6),
                          "volume_to_mean10":pos["volume_to_mean10"],
                          "volume_to_prev":pos["volume_to_prev"],
                          "signal_candle_body_pct":pos["signal_candle_body_pct"],
                          "entry_prev_color":pos["entry_prev_color"],
                          "exit_volume_to_last_same":round(vol/pos["last_same_volume"],6) if pos["last_same_volume"] else None,
                          "opposite_vol_before":pos["prev_volume"],
                          "signal_vol":pos["signal_volume"],
                          "entry_mean10":pos["mean10"],"outcome":"WIN" if pnl>0 else "LOSS" if pnl<0 else "FLAT"})
                        pos=None
            prev=rows.get(ts-FIVE)
            signal=(side is not None and prev is not None
                     and baseline.color(prev) is not None and baseline.color(prev)!=side
                     and vol>prev[4] and START<=ts+FIVE<END and
                     inst in ranking[ts] and vol>avg10)
            if pos is None and signal:
                nxt=rows.get(ts+FIVE)
                if nxt and nxt[0]>0:
                    pos={"side":side,"entry_ms":ts+FIVE,"entry_price":nxt[0],
                     "rank":ranking[ts][inst],"last_same_volume":vol,
                     "volume_to_mean10":round(vol/avg10,6),
                     "volume_to_prev":round(vol/prev[4],6) if prev[4]>0 else None,
                     "signal_candle_body_pct":round(abs(b[3]/b[0]-1)*100,6),
                     "entry_prev_color":baseline.color(prev),
                     "signal_volume":vol,"prev_volume":prev[4],"mean10":avg10}
        if pos is not None:
            alltrades.append({**pos,"coin":inst,"direction":pos["side"],"outcome":"OPEN_AT_END"})
    closed=[x for x in alltrades if x["outcome"]!="OPEN_AT_END"]
    counts=Counter(x["outcome"] for x in closed)
    print("VERIFY="+json.dumps(dict(counts)),flush=True)
    if [counts["WIN"],counts["LOSS"],counts["FLAT"]] != [41,58,6]:
        raise RuntimeError("Did not reproduce original 41/58/6; stop analysis.")
    keys=("volume_to_mean10","volume_to_prev","signal_candle_body_pct","exit_volume_to_last_same")
    byout={}
    for outcome in ("WIN","LOSS","FLAT"):
        subset=[x for x in closed if x["outcome"]==outcome]
        row={"trades":len(subset),
             "long":sum(x["direction"]=="LONG" for x in subset),
             "short":sum(x["direction"]=="SHORT" for x in subset),
             "median_rank":median([x["rank"] for x in subset])}
        for key in keys:
            nums=[float(x[key]) for x in subset if x.get(key) is not None]
            row[key]={"median":median(nums),"mean":mean(nums),
             "min":round(min(nums),4) if nums else None,
             "max":round(max(nums),4) if nums else None}
        byout[outcome]=row
    bins=[(1.0,1.25),(1.25,1.5),(1.5,2.0),(2.0,3.0),(3.0,10**9)]
    byratio=[]
    for low,high in bins:
        trade=[x for x in closed if low<=x["volume_to_mean10"]<high and x["outcome"]!="FLAT"]
        w=sum(x["outcome"]=="WIN" for x in trade)
        l=len(trade)-w
        byratio.append({"min":low,"max":high if high<1e8 else None,
          "wins":w,"losses":l,"success_pct":round(100*w/len(trade),2) if trade else None})
    byside={}
    for side in ("LONG","SHORT"):
        ss=[x for x in closed if x["direction"]==side]
        byside[side]={"wins":sum(x["outcome"]=="WIN" for x in ss),
          "losses":sum(x["outcome"]=="LOSS" for x in ss),
          "flat":sum(x["outcome"]=="FLAT" for x in ss)}
    report={"source":"Four read-only complete OHLCV shards, GitHub Actions run 39146799601, 390 current BloFin coins, 2026-10-18 16:00-17:00 UK.",
     "method":"On next open after a candle changing from red to green (LONG) or green to red (SHORT) with strictly greater volume than previous and strictly greater than arithmetic mean of previous10. Close at next open after opposing candle volume > latest same direction candle volume. Historical TOP200 by prior24h change.",
     "sample_closed":len(closed),"open_at_end":sum(x["outcome"]=="OPEN_AT_END" for x in alltrades),
     "results":dict(counts),"by_outcome":byout,"by_volume_ratio10":byratio,"by_direction":byside,
     "all_closed_trades":closed,
     "limits":"No trading fees, no spread; no independent new market sample; per-instrument concurrent trades. Historical ranks among currently-listed symbols."}
    Path("volume_rule_mean10_wins_losses_20261018.json").write_text(
      json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("VOLUME_WIN_LOSS_DETAIL="+json.dumps({k:v for k,v in report.items() if k!="all_closed_trades"},separators=(",",":"),ensure_ascii=False),flush=True)
if __name__=="__main__":
    run()
