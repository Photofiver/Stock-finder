import json, math, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot
HOUR=3600000; LIMIT=750; MIN_MATCH=300
_,_,meta=bot.get_universe(); insts=sorted(meta)

def load(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":str(LIMIT)})
        return inst,bot.parse_candles(raw)
    except: return inst,[]

series={}
with ThreadPoolExecutor(max_workers=10) as ex:
    for f in as_completed([ex.submit(load,i) for i in insts]):
        inst,b=f.result()
        if len(b)>=MIN_MATCH+1: series[inst]=b

def retmap(b):
    return {int(b[i]["ts"]+HOUR): b[i]["c"]/b[i-1]["c"]-1 for i in range(1,len(b)) if b[i-1]["c"]>0}
def corr(xs,ys):
    n=len(xs); mx=sum(xs)/n; my=sum(ys)/n
    vx=sum((x-mx)**2 for x in xs); vy=sum((y-my)**2 for y in ys)
    if not vx or not vy:return None
    return sum((x-mx)*(y-my) for x,y in zip(xs,ys))/math.sqrt(vx*vy)

eth=retmap(series["ETH-USDT"]); rows=[]
for inst,b in series.items():
    if inst=="ETH-USDT": continue
    rr=retmap(b); ks=sorted(set(eth)&set(rr))
    if len(ks)<MIN_MATCH: continue
    xs=[eth[k] for k in ks]; ys=[rr[k] for k in ks]
    c=corr(xs,ys)
    if c is None: continue
    same=100*sum(1 for x,y in zip(xs,ys) if (x>0 and y>0) or (x<0 and y<0) or (x==0 and y==0))/len(ks)
    rows.append({"inst":inst,"corr":c,"same_dir_pct":same,"n":len(ks)})
rows.sort(key=lambda x:x["corr"],reverse=True)
by={r["inst"]:r for r in rows}
top10,_,_=bot.get_universe()
current=[by[i] for i in top10 if i in by]
print("SUMMARY "+json.dumps({
 "coins_analyzed":len(rows),
 "median_corr":round(statistics.median([r["corr"] for r in rows]),3),
 "mean_corr":round(sum(r["corr"] for r in rows)/len(rows),3),
 "median_same_dir_pct":round(statistics.median([r["same_dir_pct"] for r in rows]),2),
 "current_top10":[{"inst":r["inst"],"corr":round(r["corr"],3),"same_dir_pct":round(r["same_dir_pct"],2)} for r in current],
 "top10_corr":[{"inst":r["inst"],"corr":round(r["corr"],3),"same_dir_pct":round(r["same_dir_pct"],2)} for r in rows[:10]]
},sort_keys=True))
