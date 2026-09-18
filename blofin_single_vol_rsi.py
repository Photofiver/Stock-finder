import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot

DAYS=7
HOUR=3600000
MIN=60000
HOLD=5*HOUR
FEE=0.12
TPS=[0.4,0.5,0.6,0.7,0.8,1.0,1.2]
SLS=[0.3,0.4,0.5,0.6,0.75]

_,_,meta=bot.get_universe()
insts=sorted(meta)

def load1h(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":"220"})
        b=bot.decorate(bot.parse_candles(raw))
        return inst,b if len(b)>=80 else []
    except:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=10) as ex:
    for f in as_completed([ex.submit(load1h,i) for i in insts]):
        inst,b=f.result()
        if b: series[inst]=b

latest=max((b[-1]["ts"]+HOUR for b in series.values()),default=0)
end=(latest//HOUR)*HOUR
start=end-DAYS*24*HOUR
hours=list(range(start,end,HOUR))
maps={inst:{int(x["ts"]+HOUR):(k,x) for k,x in enumerate(b)} for inst,b in series.items()}

def volside(b,i):
    if i<1:return None
    cur=b[i]; cc=bot.candle_color(cur)
    if cc=="GREEN": target="RED"; side="LONG"
    elif cc=="RED": target="GREEN"; side="SHORT"
    else:return None
    for j in range(i-1,-1,-1):
        if bot.candle_color(b[j])==target:
            return side if cur["v"]>b[j]["v"] else None
    return None

hc={}
events=set()
for t in hours:
    prev=t-24*HOUR
    ranked=[]
    for inst,b in series.items():
        cp=maps[inst].get(t); pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0: continue
        ranked.append((cp[1]["c"]/pp[1]["c"]-1,inst,cp[0]))
    ranked.sort(reverse=True)
    cs=[]
    for rank,(_,inst,i) in enumerate(ranked[:10],1):
        side=volside(series[inst],i)
        if not side: continue
        rv=series[inst][i].get("rsi")
        rside="LONG" if rv is not None and rv>50 else ("SHORT" if rv is not None and rv<50 else None)
        if rside==side:
            cs.append((rank,inst,side,t))
            events.add((inst,side,t))
    hc[t]=cs

cache={}
def load1m(ev):
    inst,side,t=ev
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1m","after":str(t+HOLD+MIN),"limit":"380"})
        a=[]
        for r in raw:
            if len(r)>=9 and str(r[8])=="1":
                ts=int(r[0])
                if t<=ts<=t+HOLD:
                    a.append((ts,float(r[1]),float(r[2]),float(r[3]),float(r[4])))
        a.sort(); return ev,a
    except:
        return ev,[]

with ThreadPoolExecutor(max_workers=12) as ex:
    for f in as_completed([ex.submit(load1m,e) for e in events]):
        ev,a=f.result(); cache[ev]=a

def outcome(ev,tp,sl):
    inst,side,t=ev
    a=cache.get(ev,[])
    if not a:return None
    e=a[0][1]
    tpv=e*(1+tp/100) if side=="LONG" else e*(1-tp/100)
    slv=e*(1-sl/100) if side=="LONG" else e*(1+sl/100)
    for ts,o,h,l,c in a:
        ht=h>=tpv if side=="LONG" else l<=tpv
        hs=l<=slv if side=="LONG" else h>=slv
        if ht and hs:return -sl,ts,True
        if hs:return -sl,ts,False
        if ht:return tp,ts,False
    ts,o,h,l,c=a[-1]
    p=((c/e)-1)*100 if side=="LONG" else ((e/c)-1)*100
    return p,ts+MIN,False

rows=[]
for tp in TPS:
    for sl in SLS:
        until=0; tr=[]
        for t in hours:
            if t<until: continue
            cs=hc.get(t) or []
            if not cs: continue
            rank,inst,side,tt=cs[0]
            o=outcome((inst,side,tt),tp,sl)
            if not o: continue
            gross,ex,amb=o
            tr.append((gross-FEE,gross,amb))
            until=ex
        n=len(tr); wins=sum(x[0]>0 for x in tr); losses=sum(x[0]<0 for x in tr)
        net=sum(x[0] for x in tr); gross=sum(x[1] for x in tr)
        rows.append({"combo":"VOL+RSI","tp":tp,"sl":sl,"trades":n,"wins":wins,"losses":losses,
                     "win_rate":round(100*wins/n,2) if n else 0,"gross_pct":round(gross,3),
                     "net_pct":round(net,3),"avg_net_pct":round(net/n,4) if n else 0,
                     "ambiguous":sum(x[2] for x in tr)})
rows.sort(key=lambda x:(x["net_pct"],x["avg_net_pct"]),reverse=True)
cur=next(x for x in rows if x["tp"]==0.6 and x["sl"]==0.5)
print("BEST "+json.dumps(rows[0],sort_keys=True))
print("CURRENT "+json.dumps(cur,sort_keys=True))
print("TOP5 "+json.dumps(rows[:5],sort_keys=True))
print("SUMMARY "+json.dumps({"days":DAYS,"hours":len(hours),"events":len(events),"best":rows[0],"current":cur},sort_keys=True))
