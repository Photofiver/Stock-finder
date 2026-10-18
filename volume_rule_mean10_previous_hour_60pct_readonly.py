"""Read-only out-of-sample 15:00-16:00 BST (2026-10-18) volume-60% test.
Reuses archived 390 BloFin public OHLCV shards; does not modify LIVE.
Frozen rule: remove every candle > arithmetic mean of previous ten volumes,
entry after opposite-colored candle when signal volume > previous volume,
optional >=1.60x previous volume, and exit on valid opposite bar volume
strictly greater than last valid same-direction bar volume. All entries
use next 5m open. TOP200 ranked by trailing 24h return at signal time.
"""
from __future__ import annotations
import gzip
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FIVE=300_000
START=int(datetime(2026,10,18,14,0,tzinfo=timezone.utc).timestamp()*1000)
END=int(datetime(2026,10,18,15,0,tzinfo=timezone.utc).timestamp()*1000)
THRESHOLDS=(1.0,1.6)
EXPECTED=390
def colour(b):
    return "LONG" if b[3]>b[0] else "SHORT" if b[3]<b[0] else None

def load():
    data={}
    for i in range(4):
        with gzip.open(Path("shards")/f"mean10_shard_{i}.json.gz","rt",encoding="utf-8") as f:
            shard=json.load(f)
        assert shard["shard"]==i
        for inst,rows in shard["data"].items():
            if inst in data:raise RuntimeError("Duplicate instrument "+inst)
            data[inst]={int(t):tuple(float(v) for v in vals) for t,vals in rows.items()}
    if len(data)!=EXPECTED:raise RuntimeError(f"Expected 390 symbols, got {len(data)}")
    return data

def ranks(data):
    by_ts={}
    counts=[]
    for ts in range(START-FIVE,END,FIVE):
        snap=[]
        prev_ts=ts-288*FIVE
        for inst,bars in data.items():
            cur=bars.get(ts)
            old=bars.get(prev_ts)
            if cur is not None and old is not None and old[3]>0:
                snap.append((cur[3]/old[3]-1,inst))
        if len(snap)<200:
            raise RuntimeError("Missing reliable TOP200 historic return ranks at "+str(ts)+"; available "+str(len(snap)))
        snap.sort(reverse=True)
        by_ts[ts]={x:k for k,(_,x) in enumerate(snap[:200],start=1)}
        counts.append(len(snap))
    return by_ts,counts

def test(data,ranking,mult):
    closed=[]
    opened=[]
    signals=0
    rejected_avg=0
    rejected_mult=0
    for inst,bars in data.items():
        position=None
        for ts in range(START-FIVE,END,FIVE):
            cur=bars.get(ts)
            if cur is None:continue
            side=colour(cur)
            preceding=[bars.get(ts-j*FIVE) for j in range(1,11)]
            if any(x is None for x in preceding):continue
            mean10=statistics.fmean(x[4] for x in preceding)
            if mean10<=0:continue
            vol=cur[4]
            eligible=vol<=mean10
            # Preserve original SKIP_ABOVE_MEAN10_ALL exit semantics.
            if position is not None and ts>=position["entry_ms"] and side is not None and eligible:
                if side==position["side"]:
                    position["last_same_vol"]=vol
                elif vol>position["last_same_vol"]:
                    nxt=bars.get(ts+FIVE)
                    if nxt is not None and ts+FIVE<=END:
                        gross=100*(nxt[0]/position["entry_price"]-1)*(1 if position["side"]=="LONG" else -1)
                        closed.append({
                            "coin":inst,"side":position["side"],"rank":position["rank"],
                            "entry_ts":position["entry_ms"],"exit_ts":ts+FIVE,
                            "volume_ratio_prev":position["volume_ratio_prev"],
                            "volume_ratio_mean10":position["volume_ratio_mean10"],
                            "gross_pct":round(gross,6),
                            "outcome":"WIN" if gross>0 else "LOSS" if gross<0 else "FLAT"})
                        position=None
            old=bars.get(ts-FIVE)
            signal=(side is not None and old is not None and colour(old) is not None
                    and colour(old)!=side and vol>old[4] and
                    START<=ts+FIVE<END and inst in ranking[ts])
            if signal:
                signals+=1
                if not eligible:
                    rejected_avg+=1
                elif old[4]>0 and vol<old[4]*mult:
                    rejected_mult+=1
            if position is None and signal and eligible and (old[4]<=0 or vol>=old[4]*mult):
                nxt=bars.get(ts+FIVE)
                if nxt is not None and nxt[0]>0:
                    position={
                      "side":side,"entry_ms":ts+FIVE,"entry_price":nxt[0],
                      "rank":ranking[ts][inst],"last_same_vol":vol,
                      "volume_ratio_prev":round(vol/old[4],5) if old[4]>0 else None,
                      "volume_ratio_mean10":round(vol/mean10,5)}
        if position is not None:
            opened.append({"coin":inst,"side":position["side"]})
    total=Counter(t["outcome"] for t in closed)
    wins=total["WIN"]; losses=total["LOSS"]; flat=total["FLAT"]
    assert wins+losses+flat==len(closed)
    by_side={}
    for side in ("LONG","SHORT"):
        s=[t for t in closed if t["side"]==side]
        c=Counter(t["outcome"] for t in s)
        by_side[side]={"wins":c["WIN"],"losses":c["LOSS"],"flat":c["FLAT"],
                       "winrate_pct_excluding_flat":round(100*c["WIN"]/(c["WIN"]+c["LOSS"]),2) if c["WIN"]+c["LOSS"] else None}
    result={
      "minimum_entry_volume_ratio_prev":mult,
      "candidate_signals":signals,"signals_rejected_mean10":rejected_avg,
      "signals_rejected_additional_60pct":rejected_mult,
      "closed":len(closed),"wins":wins,"losses":losses,"flat":flat,
      "open_at_hour_end":len(opened),
      "winrate_pct_excluding_flat":round(100*wins/(wins+losses),2) if wins+losses else None,
      "winrate_pct_closed_including_flat":round(100*wins/len(closed),2) if closed else None,
      "gross_sum_pct_points":round(sum(x["gross_pct"] for x in closed),5),
      "long_short":by_side,
      "trades":closed,
      "open_coins":opened,
    }
    print("HOUR60_RESULT="+json.dumps({k:v for k,v in result.items() if k not in ("trades","open_coins")},separators=(",",":"),ensure_ascii=False),flush=True)
    return result

def main():
    data=load()
    ranking,coverage=ranks(data)
    print(f"DATA_READY symbols={len(data)} snapshots={len(coverage)} minimum_ranked={min(coverage)}",flush=True)
    scores={("baseline" if mult==1.0 else "require_60pct_more_volume"):test(data,ranking,mult) for mult in THRESHOLDS}
    report={
     "period_uk":"2026-10-18 15:00-16:00 BST",
     "source":"390 public BloFin crypto USDT swaps, archived OHLCV, no live mutation",
     "ranking":"TOP200 among all currently listed eligible crypto USDT swaps each 5m candle using trailing24h close price change",
     "mean10":"Skip every candle whose volume exceeds mean of prior ten completed 5m volume candles, both for entry and for exit/update",
     "entry":"Consecutive opposite colour bars, second volume strictly greater than first, buy/sell at next 5m bar open. 60pct test adds second volume >=1.6 times first.",
     "exit":"At next 5m open after eligible opposite colour bar volume exceeds most recent eligible same colour volume.",
     "fee_pct":0,"spread_slippage_not_included":True,
     "ranking_coverage_min":min(coverage),
     "trading":"Independent per-coin positions may overlap. Open outcomes at hour end excluded from win rate. No external TP or SL. No trading orders placed.",
     "results":scores,
    }
    out=Path("volume_rule_mean10_previous_hour_60pct_result.json")
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("DETAILS_SAVED="+str(out),flush=True)
if __name__=="__main__":main()
