"""Read-only 4H candle reconstruction from persisted verified BloFin 5m JSON reviews.
No network/API credentials, no LIVE modifications, no fabricated missing candles.
"""
import collections
import gc
import json
from pathlib import Path
from datetime import datetime, timezone
import research_real_4h_profit_readonly as test

FIVE=300000
FOUR=test.FOUR

def archive():
    files=sorted(Path("blofin_live_scans").glob("live_watch_history_5m*.json"))
    if len(files)<15:raise RuntimeError("Missing archived watch-history shards")
    rows=collections.defaultdict(dict)
    rank=collections.defaultdict(dict)
    coverage=collections.Counter()
    for fi,path in enumerate(files,1):
        with path.open(encoding="utf8") as f:obj=json.load(f)
        for scan in obj.get("scans") or []:
            close=int(scan.get("signal_close_ms") or 0)
            if close<=0:continue
            for item in scan.get("instruments") or []:
                inst=item.get("inst")
                if not inst:continue
                own=int(item.get("signal_close_ms") or 0)
                if own==close and isinstance(item.get("rank"),int):
                    rank[inst][close]=item["rank"]
                nxt=item.get("next_candle_review") or {}
                if nxt.get("status")!="DONE":continue
                nextclose=int(nxt.get("next_candle_close_ms") or 0)
                if nextclose!=close+FIVE:
                    coverage["review_bad_time"]+=1
                    continue
                try:
                    b=(float(nxt["open"]),float(nxt["high"]),float(nxt["low"]),
                       float(nxt["close"]),float(nxt["indicators_after_5m"]["volume"]))
                except (KeyError,ValueError,TypeError):
                    coverage["review_incomplete"]+=1
                    continue
                if min(b[:4])<=0 or b[4]<0 or b[2]>min(b[0],b[3]) or b[1]<max(b[0],b[3]):
                    coverage["review_bad_ohlc"]+=1
                    continue
                t=nextclose-FIVE
                old=rows[inst].get(t)
                if old is not None and old!=b:
                    coverage["review_conflicts"]+=1
                    # Do not keep conflicting candles.
                    continue
                rows[inst][t]=b
                coverage["5m_candles_accepted"]+=1
        if fi%5==0 or fi==len(files):
            print("ARCHIVE_LOAD="+json.dumps({"files":fi,"total":len(files),"coins":len(rows),"bars":coverage["5m_candles_accepted"]}),flush=True)
        del obj
        gc.collect()
    return rows,rank,coverage

def aggregate(rows):
    outputs={}
    for inst,candles in rows.items():
        grouped=collections.defaultdict(list)
        for t,b in candles.items():
            grouped[t//FOUR*FOUR].append((t,b))
        four={}
        for t,items in grouped.items():
            if len(items)!=48:continue
            items.sort(key=lambda x:x[0])
            if any(items[i][0]!=t+i*FIVE for i in range(48)):
                continue
            first=items[0][1];last=items[-1][1]
            four[t]=(first[0],max(x[1][1] for x in items),
                     min(x[1][2] for x in items),last[3],sum(x[1][4] for x in items))
        if four:outputs[inst]=dict(sorted(four.items()))
    return outputs

def main():
    raw,ranks,counts=archive()
    candles=aggregate(raw)
    sigs=[]
    eligible=0
    for inst,four in candles.items():
        entries=test.signals(inst,sorted(four.items()))
        eligible+=len(entries)
        for entry in entries:
            # Use historical recorded TOP200 rank at the 4h signal close.
            closed=entry[0]
            if 1<=ranks.get(inst,{}).get(closed,999)<=200:
                sigs.append(entry)
    sigs.sort(key=lambda x:(x[0],x[1],x[2]))
    alltimes=[t for m in candles.values() for t in m]
    result={"mode":"archived_verified_5m_into_closed_4h_only","status":"COVERAGE_ONLY_NOT_LIVE",
            "coins_in_archive":len(raw),"coins_with_complete_4h":len(candles),
            "bars_5m_accepted":counts["5m_candles_accepted"],
            "fully_completed_4h_bars":len(alltimes),
            "raw_4h_signals":eligible,"historical_top200_ranked_signals":len(sigs),
            "limitations":"Only verified next_candle_review rows; gaps are not filled. 4H signal requires 61 consecutive 4h candles (10+ days). If coverage sparse, NO backtest profitability result can be produced."}
    # At least 12 non-overlapping signals are needed even to report exploratory results.
    if len(sigs)>=12 and alltimes:
        first=min(alltimes);end=max(alltimes)+FOUR
        split=((first+(end-first)*6//10)//FOUR)*FOUR
        result["status"]="PRELIMINARY_BACKTEST_NOT_LIVE"
        result["period"]=[datetime.fromtimestamp(first/1000,timezone.utc).isoformat(),datetime.fromtimestamp(end/1000,timezone.utc).isoformat()]
        result["splits"]={}
        for name,tp,sl in [("tp1_5_sl1",1.5,1),("tp2_sl1",2,1),("tp2_sl0_7",2,.7)]:
            result["splits"][name]={"train":test.simulate(sigs,candles,first,split,tp,sl),
                                   "holdout":test.simulate(sigs,candles,split,end,tp,sl)}
        if any(x["holdout"]["trades"]<5 for x in result["splits"].values()):
            result["status"]="INSUFFICIENT_HOLDOUT_TRADES"
    print("ARCHIVED_4H_RESEARCH_RESULT="+json.dumps(result,sort_keys=True),flush=True)
    with open("research_saved_true4h_result.json","w") as f:json.dump(result,f,indent=2)

if __name__=="__main__":main()
