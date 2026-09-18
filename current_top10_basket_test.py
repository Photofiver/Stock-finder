import json, statistics
import blofin_live_hourly as bot

H=3600000
top10,_,_=bot.get_universe()
print("TOP10",top10)

series={}
for inst in top10:
    raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":"750"})
    b=bot.parse_candles(raw)
    if len(b)>=300: series[inst]=b

maps={inst:{int(x["ts"]+H):x for x in b} for inst,b in series.items()}
times=sorted(set.intersection(*(set(m.keys()) for m in maps.values()))) if maps else []
times=times[1:-1]

same_now=0; same_next=0; n=0
up_n=up_ok=down_n=down_ok=0
per={i:{"n":0,"now":0,"next":0} for i in series}

for t in times:
    cur={}
    nxt={}
    for inst in series:
        a=maps[inst].get(t-H); b=maps[inst].get(t); c=maps[inst].get(t+H)
        if not a or not b or not c or a["c"]<=0 or b["c"]<=0: continue
        cur[inst]=b["c"]/a["c"]-1
        nxt[inst]=c["c"]/b["c"]-1
    if len(cur)<8: continue

    for inst in list(cur):
        others=[v for k,v in cur.items() if k!=inst]
        sector=sum(others)/len(others)
        ss=1 if sector>0 else (-1 if sector<0 else 0)
        cs=1 if cur[inst]>0 else (-1 if cur[inst]<0 else 0)
        ns=1 if nxt[inst]>0 else (-1 if nxt[inst]<0 else 0)
        if ss==0: continue
        n+=1
        per[inst]["n"]+=1
        if ss==cs:
            same_now+=1; per[inst]["now"]+=1
        if ss==ns:
            same_next+=1; per[inst]["next"]+=1
        if ss>0:
            up_n+=1
            if ns>0: up_ok+=1
        else:
            down_n+=1
            if ns<0: down_ok+=1

print("SUMMARY "+json.dumps({
 "top10":top10,
 "coins_with_data":list(series),
 "observations":n,
 "same_hour_pct":round(100*same_now/n,2) if n else None,
 "next_hour_pct":round(100*same_next/n,2) if n else None,
 "sector_up_next_up_pct":round(100*up_ok/up_n,2) if up_n else None,
 "sector_down_next_down_pct":round(100*down_ok/down_n,2) if down_n else None,
 "per_coin":{k:{
    "n":v["n"],
    "same_hour_pct":round(100*v["now"]/v["n"],2) if v["n"] else None,
    "next_hour_pct":round(100*v["next"]/v["n"],2) if v["n"] else None
 } for k,v in per.items()}
},sort_keys=True))
