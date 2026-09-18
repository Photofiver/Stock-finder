import json, math, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot

HOUR=3600000
LIMIT=750
MIN_MATCH=300

_,_,meta=bot.get_universe()
insts=sorted(meta)

def load(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":str(LIMIT)})
        b=bot.parse_candles(raw)
        return inst,b
    except:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=10) as ex:
    for f in as_completed([ex.submit(load,i) for i in insts]):
        inst,b=f.result()
        if len(b)>=MIN_MATCH+1:
            series[inst]=b

btc=series.get("BTC-USDT",[])
if len(btc)<MIN_MATCH+1:
    raise RuntimeError("BTC-USDT data unavailable")

def returns_map(bars):
    out={}
    for i in range(1,len(bars)):
        p=bars[i-1]["c"]; c=bars[i]["c"]
        if p>0:
            out[int(bars[i]["ts"]+HOUR)] = c/p - 1.0
    return out

btc_r=returns_map(btc)

def pearson(xs,ys):
    n=len(xs)
    if n<2:return None
    mx=sum(xs)/n; my=sum(ys)/n
    vx=sum((x-mx)**2 for x in xs)
    vy=sum((y-my)**2 for y in ys)
    if vx<=0 or vy<=0:return None
    cov=sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    return cov/math.sqrt(vx*vy)

rows=[]
for inst,bars in series.items():
    if inst=="BTC-USDT": continue
    rr=returns_map(bars)
    keys=sorted(set(btc_r).intersection(rr))
    if len(keys)<MIN_MATCH: continue
    xs=[btc_r[k] for k in keys]
    ys=[rr[k] for k in keys]
    corr=pearson(xs,ys)
    if corr is None: continue
    same=sum(1 for x,y in zip(xs,ys) if (x>0 and y>0) or (x<0 and y<0) or (x==0 and y==0))
    directional=100*same/len(keys)
    beta_den=sum(x*x for x in xs)
    beta=(sum(x*y for x,y in zip(xs,ys))/beta_den) if beta_den else None
    rows.append({"inst":inst,"corr":corr,"same_dir_pct":directional,"beta":beta,"n":len(keys)})

rows.sort(key=lambda x:x["corr"],reverse=True)
corrs=[r["corr"] for r in rows]
dirs=[r["same_dir_pct"] for r in rows]

buckets={
 "corr_ge_0_7":sum(r["corr"]>=0.7 for r in rows),
 "corr_0_4_to_0_7":sum(0.4<=r["corr"]<0.7 for r in rows),
 "corr_0_2_to_0_4":sum(0.2<=r["corr"]<0.4 for r in rows),
 "corr_0_to_0_2":sum(0<=r["corr"]<0.2 for r in rows),
 "corr_negative":sum(r["corr"]<0 for r in rows)
}

top10,_,_=bot.get_universe()
top10_rows=[]
by={r["inst"]:r for r in rows}
for inst in top10:
    if inst in by:
        top10_rows.append(by[inst])

summary={
 "coins_analyzed":len(rows),
 "hours_per_coin_min":MIN_MATCH,
 "median_corr":round(statistics.median(corrs),3) if corrs else None,
 "mean_corr":round(sum(corrs)/len(corrs),3) if corrs else None,
 "median_same_dir_pct":round(statistics.median(dirs),2) if dirs else None,
 "mean_same_dir_pct":round(sum(dirs)/len(dirs),2) if dirs else None,
 "buckets":buckets,
 "top_10_most_correlated":[{**r,"corr":round(r["corr"],3),"same_dir_pct":round(r["same_dir_pct"],2),"beta":round(r["beta"],2) if r["beta"] is not None else None} for r in rows[:10]],
 "bottom_10":[{**r,"corr":round(r["corr"],3),"same_dir_pct":round(r["same_dir_pct"],2),"beta":round(r["beta"],2) if r["beta"] is not None else None} for r in rows[-10:]],
 "current_top10":[{**r,"corr":round(r["corr"],3),"same_dir_pct":round(r["same_dir_pct"],2),"beta":round(r["beta"],2) if r["beta"] is not None else None} for r in top10_rows]
}
print("SUMMARY "+json.dumps(summary,sort_keys=True))
