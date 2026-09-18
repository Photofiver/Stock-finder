import json, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot

HOUR=3600000
LIMIT=750
MIN_BARS=600

_,_,meta=bot.get_universe()
insts=sorted(meta)

def load(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":str(LIMIT)})
        b=bot.parse_candles(raw)
        return inst,b
    except Exception:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=10) as ex:
    for f in as_completed([ex.submit(load,i) for i in insts]):
        inst,b=f.result()
        if len(b)>=MIN_BARS:
            series[inst]=b

maps={}
for inst,b in series.items():
    maps[inst]={int(x["ts"]+HOUR):(i,x) for i,x in enumerate(b)}

all_times=sorted(set().union(*(set(m.keys()) for m in maps.values())))
# Need 24h history and 1h future.
times=all_times[24:-1]

records=[]
for t in times:
    prev=t-24*HOUR
    ranked=[]
    for inst,b in series.items():
        cp=maps[inst].get(t); pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0: continue
        change24=cp[1]["c"]/pp[1]["c"]-1
        ranked.append((change24,inst))
    ranked.sort(reverse=True)
    top=[inst for _,inst in ranked[:10]]
    if len(top)<10: continue

    # Current 1h return for each top10 constituent.
    cur_rets={}
    next_rets={}
    for inst in top:
        cur=maps[inst].get(t)
        prev1=maps[inst].get(t-HOUR)
        nxt=maps[inst].get(t+HOUR)
        if cur and prev1 and prev1[1]["c"]>0:
            cur_rets[inst]=cur[1]["c"]/prev1[1]["c"]-1
        if cur and nxt and cur[1]["c"]>0:
            next_rets[inst]=nxt[1]["c"]/cur[1]["c"]-1

    if len(cur_rets)<8: continue

    sector_all=sum(cur_rets.values())/len(cur_rets)

    for inst in top:
        if inst not in cur_rets or inst not in next_rets: continue
        others=[r for k,r in cur_rets.items() if k!=inst]
        if len(others)<7: continue
        sector_excl=sum(others)/len(others)
        target_cur=cur_rets[inst]
        target_next=next_rets[inst]

        sector_sign=1 if sector_excl>0 else (-1 if sector_excl<0 else 0)
        cur_sign=1 if target_cur>0 else (-1 if target_cur<0 else 0)
        next_sign=1 if target_next>0 else (-1 if target_next<0 else 0)

        records.append({
            "t":t,"inst":inst,
            "sector":sector_excl,
            "cur":target_cur,
            "next":target_next,
            "same_now": sector_sign!=0 and sector_sign==cur_sign,
            "same_next": sector_sign!=0 and sector_sign==next_sign,
            "sector_up":sector_sign>0,
            "sector_down":sector_sign<0,
            "next_up":next_sign>0,
            "next_down":next_sign<0
        })

n=len(records)
same_now=sum(r["same_now"] for r in records)
same_next=sum(r["same_next"] for r in records)
up=[r for r in records if r["sector_up"]]
down=[r for r in records if r["sector_down"]]

summary={
  "series":len(series),
  "observations":n,
  "same_hour_agreement_pct":round(100*same_now/n,2) if n else None,
  "next_hour_agreement_pct":round(100*same_next/n,2) if n else None,
  "when_sector_up_next_coin_up_pct":round(100*sum(r["next_up"] for r in up)/len(up),2) if up else None,
  "when_sector_down_next_coin_down_pct":round(100*sum(r["next_down"] for r in down)/len(down),2) if down else None,
  "sector_up_obs":len(up),
  "sector_down_obs":len(down),
}

# Strategy-entry-specific test: current top10 + strict volume-flip signal,
# check whether sector direction aligned with signal and whether next hour followed signal.
entry=[]
for t in times:
    prev=t-24*HOUR
    ranked=[]
    for inst,b in series.items():
        cp=maps[inst].get(t); pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0: continue
        ranked.append((cp[1]["c"]/pp[1]["c"]-1,inst))
    ranked.sort(reverse=True)
    top=[inst for _,inst in ranked[:10]]
    if len(top)<10: continue

    cur_rets={}
    for inst in top:
        cur=maps[inst].get(t); p1=maps[inst].get(t-HOUR)
        if cur and p1 and p1[1]["c"]>0:
            cur_rets[inst]=cur[1]["c"]/p1[1]["c"]-1

    for inst in top:
        cp=maps[inst].get(t)
        pp=maps[inst].get(t-HOUR)
        nxt=maps[inst].get(t+HOUR)
        if not cp or not pp or not nxt or inst not in cur_rets: continue
        bprev=pp[1]; bcur=cp[1]
        pc=bot.candle_color(bprev); cc=bot.candle_color(bcur)
        side=None
        if pc=="RED" and cc=="GREEN" and bcur["v"]>bprev["v"]: side="LONG"
        elif pc=="GREEN" and cc=="RED" and bcur["v"]>bprev["v"]: side="SHORT"
        if not side: continue
        others=[r for k,r in cur_rets.items() if k!=inst]
        if len(others)<7: continue
        sector=sum(others)/len(others)
        aligned=(side=="LONG" and sector>0) or (side=="SHORT" and sector<0)
        nextret=nxt[1]["c"]/bcur["c"]-1 if bcur["c"]>0 else 0
        followed=(side=="LONG" and nextret>0) or (side=="SHORT" and nextret<0)
        entry.append({"aligned":aligned,"followed":followed,"side":side,"sector":sector,"nextret":nextret})

ea=[r for r in entry if r["aligned"]]
en=[r for r in entry if not r["aligned"]]
summary["entry_signals"]=len(entry)
summary["aligned_entry_signals"]=len(ea)
summary["aligned_next_hour_success_pct"]=round(100*sum(r["followed"] for r in ea)/len(ea),2) if ea else None
summary["not_aligned_next_hour_success_pct"]=round(100*sum(r["followed"] for r in en)/len(en),2) if en else None

print("SUMMARY "+json.dumps(summary,sort_keys=True))
