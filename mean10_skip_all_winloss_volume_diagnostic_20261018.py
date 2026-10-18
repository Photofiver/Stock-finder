"""READ ONLY. Compare winning/losing volumes for SKIP_ABOVE_MEAN10_ALL on Oct 18 16-17 UK.
Exactly reproduce the frozen historical backtest, with same ranked TOP200 subset and exits.
Never call private APIs or write LIVE files.
"""
from __future__ import annotations
import gzip
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import volume_rule_mean10_hour_20261018_readonly as m
FIVE=m.STEP; START=m.START; END=m.END
UK=ZoneInfo("Europe/London")

def load():
    data={}
    for n in range(4):
        with gzip.open(Path("shards")/f"mean10_shard_{n}.json.gz","rt") as f:
            shard=json.load(f)
        for inst,rows in shard["data"].items():
            if inst in data: raise RuntimeError("duplicate")
            data[inst]={int(k):tuple(v) for k,v in rows.items()}
    if len(data)!=390: raise RuntimeError(f"Missing symbols: {len(data)}")
    return data

def median(trades,key):
    vals=[float(t[key]) for t in trades if t.get(key) is not None]
    return round(statistics.median(vals),4) if vals else None

def average(trades,key):
    vals=[float(t[key]) for t in trades if t.get(key) is not None]
    return round(statistics.fmean(vals),4) if vals else None

def bins(trades,key,edges):
    output=[]
    for lo,hi in zip(edges,edges[1:]):
        chunk=[t for t in trades if t.get(key) is not None and lo<=t[key]<hi and t["label"] in ("WIN","LOSS")]
        w=sum(x["label"]=="WIN" for x in chunk)
        l=sum(x["label"]=="LOSS" for x in chunk)
        output.append({"range":[lo,hi if hi<1e10 else "inf"],
                       "wins":w,"losses":l,
                       "winrate_pct":round(100*w/len(chunk),2) if chunk else None})
    return output

def main():
    data=load()
    rank_at={}
    for ts in range(START-FIVE,END,FIVE):
        vals=[]
        for inst,rows in data.items():
            cur=rows.get(ts); old=rows.get(ts-288*FIVE)
            if cur is not None and old is not None and old[3]>0:
                vals.append((cur[3]/old[3]-1,inst))
        if len(vals)<200:raise RuntimeError("Too few ranked coins")
        vals.sort(reverse=True)
        rank_at[ts]={x:i for i,(_,x) in enumerate(vals[:200],1)}
    trades=[]
    for inst,rows in data.items():
        pos=None
        for ts in range(START-FIVE,END,FIVE):
            b=rows.get(ts)
            if b is None:continue
            side=m.color(b)
            ten=[rows.get(ts-j*FIVE) for j in range(1,11)]
            if any(z is None for z in ten):continue
            avg=statistics.fmean(z[4] for z in ten)
            if avg<=0:continue
            vol=b[4]
            # Skip any bar above previous 10-bar volume arithmetic MEAN.
            ignored=vol>avg
            if pos is not None and ts>=pos["entry_ms"] and side is not None and not ignored:
                if side==pos["side"]:
                    pos["last_same_volume"]=vol
                elif vol>pos["last_same_volume"]:
                    nxt=rows.get(ts+FIVE)
                    if nxt is not None and ts+FIVE<=END:
                        sign=1 if pos["side"]=="LONG" else -1
                        pct=100*sign*(nxt[0]/pos["entry_price"]-1)
                        trades.append({
                            "inst":inst,"rank":pos["rank"],"side":pos["side"],
                            "entry_ms":pos["entry_ms"],"exit_ms":ts+FIVE,
                            "entry_clock":datetime.fromtimestamp(pos["entry_ms"]/1000,UK).strftime("%H:%M"),
                            "exit_clock":datetime.fromtimestamp((ts+FIVE)/1000,UK).strftime("%H:%M"),
                            "profit_pct":round(pct,6),
                            "label":"WIN" if pct>0 else "LOSS" if pct<0 else "FLAT",
                            "entry_vol_mean10_ratio":pos["entry_vol_mean10_ratio"],
                            "entry_vol_prev_ratio":pos["entry_vol_prev_ratio"],
                            "entry_vol_prev_diff_pct":pos["entry_vol_prev_diff_pct"],
                            "entry_vol_abs":pos["entry_vol_abs"],
                            "entry_mean10":pos["entry_mean10"],
                            "entry_candle_body_pct":pos["entry_candle_body_pct"],
                            "exit_vol_mean10_ratio":round(vol/avg,6),
                            "exit_vol_last_same_ratio":round(vol/pos["last_same_volume"],6) if pos["last_same_volume"]>0 else None,
                            "exit_vol_entry_ratio":round(vol/pos["entry_vol_abs"],6) if pos["entry_vol_abs"]>0 else None,
                            "entry_peak_same_vol":pos["last_same_volume"],
                            "exit_vol_abs":vol,
                        })
                        pos=None
            old=rows.get(ts-FIVE)
            signal=(side is not None and old is not None and m.color(old) is not None
                    and m.color(old)!=side and vol>old[4]
                    and START<=ts+FIVE<END and inst in rank_at[ts])
            if pos is None and signal and not ignored:
                nxt=rows.get(ts+FIVE)
                if nxt and nxt[0]>0:
                    pos={"side":side,"entry_ms":ts+FIVE,"entry_price":nxt[0],
                         "rank":rank_at[ts][inst],
                         "entry_vol_mean10_ratio":round(vol/avg,6),
                         "entry_vol_prev_ratio":round(vol/old[4],6) if old[4] else None,
                         "entry_vol_prev_diff_pct":round(100*(vol/old[4]-1),6) if old[4] else None,
                         "entry_candle_body_pct":round(100*abs(b[3]/b[0]-1),6),
                         "entry_vol_abs":vol,"entry_mean10":avg,
                         "last_same_volume":vol}
        if pos is not None:
            trades.append({"inst":inst,"label":"OPEN_AT_END","side":pos["side"]})
    counts=Counter(t["label"] for t in trades)
    print("REPRODUCED="+json.dumps(counts),flush=True)
    if counts["WIN"]!=37 or counts["LOSS"]!=59 or counts["FLAT"]!=3 or counts["OPEN_AT_END"]!=112:
        raise RuntimeError("MISMATCH: stop before presenting unreconciled findings")
    closed=[t for t in trades if t["label"]!="OPEN_AT_END"]
    fields=["entry_vol_mean10_ratio","entry_vol_prev_ratio","entry_vol_prev_diff_pct",
            "exit_vol_mean10_ratio","exit_vol_last_same_ratio","exit_vol_entry_ratio","entry_candle_body_pct"]
    by_label={}
    for label in ("WIN","LOSS","FLAT"):
        subset=[x for x in closed if x["label"]==label]
        by_label[label]={"n":len(subset),
             "long":sum(x["side"]=="LONG" for x in subset),
             "short":sum(x["side"]=="SHORT" for x in subset),
             "median_rank":median(subset,"rank"),
             "features":{key:{"median":median(subset,key),"mean":average(subset,key)} for key in fields}}
    byside={}
    for side in ("LONG","SHORT"):
        ss=[t for t in closed if t["side"]==side]
        byside[side]={lab:sum(t["label"]==lab for t in ss) for lab in ("WIN","LOSS","FLAT")}
    binned={
        "entry_vol_mean10_ratio":bins(closed,"entry_vol_mean10_ratio",[0,.3,.5,.7,.85,1.000001]),
        "entry_vol_prev_ratio":bins(closed,"entry_vol_prev_ratio",[1,1.2,1.5,2,3,5,10,1e12]),
        "exit_vol_entry_ratio":bins(closed,"exit_vol_entry_ratio",[0,.5,.75,1,1.25,1.5,2,3,1e12]),
        "exit_vol_last_same_ratio":bins(closed,"exit_vol_last_same_ratio",[1,1.2,1.5,2,3,10,1e12]),
    }
    report={"period_uk":"2026-10-18 16:00-17:00",
        "sample":"390 complete eligible BloFin USDT futures, TOP200 ranked point-in-time",
        "rule":"SKIP_ABOVE_MEAN10_ALL; ignore ALL candles with volume>arithmetic mean previous 10; entries only after opposite-colored bar followed by larger same-direction volume; exit after opposite-color valid bar exceeds last valid same-color bar volume.",
        "counts":dict(counts),"by_outcome":by_label,"by_direction":byside,
        "bins":binned,
        "all_closed_trades":closed,
        "warning":"Volume may correlate with outcome but causality cannot be inferred, one hour sample; fees excluded; concurrent positions per symbol; ranking reconstructed using currently-listed markets."}
    Path("mean10_skip_all_winloss_volume_diagnostic_20261018.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("WINLOSS_VOLUME_RESULT="+json.dumps({k:v for k,v in report.items() if k!="all_closed_trades"},
          ensure_ascii=False,separators=(",",":")),flush=True)

if __name__=="__main__":main()
