import json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot

DAYS=30
HOUR=3600000
MIN=60000
HOLD=5*HOUR
FEE=0.12
MULTS=[1.25,1.5,2.0,3.0]
TPS=[0.4,0.5,0.6,0.7,0.8,1.0,1.2,1.5]
SLS=[0.3,0.4,0.5,0.6,0.75,1.0]

_,_,meta=bot.get_universe()
insts=sorted(meta)
print("UNIVERSE",len(insts))

def load1h(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":"900"})
        b=bot.decorate(bot.parse_candles(raw))
        return inst,b if len(b)>=50 else []
    except Exception:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=8) as ex:
    futs=[ex.submit(load1h,x) for x in insts]
    for n,f in enumerate(as_completed(futs),1):
        inst,b=f.result()
        if b: series[inst]=b
        if n%50==0: print("1H_PROGRESS",n,len(series))

latest=max((b[-1]["ts"]+HOUR for b in series.values()),default=0)
end=(latest//HOUR)*HOUR
start=end-DAYS*24*HOUR
hours=list(range(start,end,HOUR))
maps={i:{int(x["ts"]+HOUR):(k,x) for k,x in enumerate(b)} for i,b in series.items()}
print("PERIOD",start,end,"HOURS",len(hours),"SERIES",len(series))

def vol_ratio_and_side(bars,i):
    if i<1: return None,None
    cur=bars[i]; cc=bot.candle_color(cur)
    if cc=="GREEN":
        target="RED"; side="LONG"
    elif cc=="RED":
        target="GREEN"; side="SHORT"
    else:
        return None,None
    prev=None
    for j in range(i-1,-1,-1):
        if bot.candle_color(bars[j])==target:
            prev=bars[j]; break
    if prev is None or prev["v"]<=0: return None,None
    return cur["v"]/prev["v"], side

hour_ranked={}
base_events=set()
for idx,t in enumerate(hours,1):
    prev=t-24*HOUR
    rank=[]
    for inst,b in series.items():
        cp=maps[inst].get(t); pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0: continue
        rank.append((cp[1]["c"]/pp[1]["c"]-1,inst,cp[0]))
    rank.sort(reverse=True)
    rows=[]
    for r,(_,inst,i) in enumerate(rank[:10],1):
        ratio,side=vol_ratio_and_side(series[inst],i)
        if ratio is not None and ratio>=1.25:
            rows.append((r,inst,side,t,ratio))
            base_events.add((inst,side,t))
    hour_ranked[t]=rows
    if idx%100==0: print("SIGNAL_PROGRESS",idx,"EVENTS",len(base_events))
print("BASE_EVENTS",len(base_events))

cache={}
def load1m(ev):
    inst,side,t=ev
    try:
        raw=bot.market_get("/api/v1/market/candles",{
            "instId":inst,"bar":"1m","after":str(t+HOLD+MIN),"limit":"380"})
        a=[]
        for r in raw:
            try:
                if len(r)>=9 and str(r[8])=="1":
                    ts=int(r[0])
                    if t<=ts<=t+HOLD:
                        a.append((ts,float(r[1]),float(r[2]),float(r[3]),float(r[4])))
            except: pass
        a.sort()
        return ev,a
    except Exception:
        return ev,[]

with ThreadPoolExecutor(max_workers=8) as ex:
    futs=[ex.submit(load1m,e) for e in base_events]
    for n,f in enumerate(as_completed(futs),1):
        ev,a=f.result(); cache[ev]=a
        if n%100==0: print("1M_PROGRESS",n,len(base_events))

def outcome(ev,tp,sl):
    inst,side,t=ev
    a=cache.get(ev,[])
    if not a: return None
    entry=a[0][1]
    tpv=entry*(1+tp/100) if side=="LONG" else entry*(1-tp/100)
    slv=entry*(1-sl/100) if side=="LONG" else entry*(1+sl/100)
    for ts,o,h,l,c in a:
        ht=h>=tpv if side=="LONG" else l<=tpv
        hs=l<=slv if side=="LONG" else h>=slv
        if ht and hs: return -sl,ts,True,"AMBIG_SL"
        if hs: return -sl,ts,False,"SL"
        if ht: return tp,ts,False,"TP"
    ts,o,h,l,c=a[-1]
    p=((c/entry)-1)*100 if side=="LONG" else ((entry/c)-1)*100
    return p,ts+MIN,False,"MAX5H"

all_results=[]
best_by_mult={}
current_by_mult={}
for mult in MULTS:
    cfgs=[]
    for tp in TPS:
      for sl in SLS:
        until=0; tr=[]
        for t in hours:
            if t<until: continue
            candidates=[x for x in (hour_ranked.get(t) or []) if x[4]>=mult]
            if not candidates: continue
            r,inst,side,tt,ratio=candidates[0]
            o=outcome((inst,side,tt),tp,sl)
            if not o: continue
            gross,exit_ts,amb,kind=o
            tr.append((gross-FEE,gross,amb,kind,inst,side,tt,ratio))
            until=exit_ts
        n=len(tr); wins=sum(x[0]>0 for x in tr); losses=sum(x[0]<0 for x in tr)
        net=sum(x[0] for x in tr); gross=sum(x[1] for x in tr); amb=sum(x[2] for x in tr)
        row={"mult":mult,"tp":tp,"sl":sl,"trades":n,"wins":wins,"losses":losses,
          "ambiguous":amb,"win_rate":round(100*wins/n,2) if n else 0,
          "gross_pct":round(gross,3),"net_pct":round(net,3),
          "avg_net_pct":round(net/n,4) if n else 0}
        cfgs.append(row); all_results.append(row)
    cfgs.sort(key=lambda x:(x["net_pct"],x["avg_net_pct"],x["trades"]),reverse=True)
    best_by_mult[str(mult)]=cfgs[0]
    current_by_mult[str(mult)]=next(x for x in cfgs if x["tp"]==0.6 and x["sl"]==0.5)
    print("BEST_MULT "+json.dumps(cfgs[0],sort_keys=True))
    print("CURRENT_MULT "+json.dumps(current_by_mult[str(mult)],sort_keys=True))

all_results.sort(key=lambda x:(x["net_pct"],x["avg_net_pct"],x["trades"]),reverse=True)
print("TOP_RESULTS "+json.dumps(all_results[:20],sort_keys=True))
print("SUMMARY "+json.dumps({
 "days":DAYS,"hours":len(hours),"series":len(series),"base_events":len(base_events),
 "positive_configs":sum(x["net_pct"]>0 for x in all_results),
 "best_overall":all_results[0] if all_results else None,
 "best_by_mult":best_by_mult,
 "current_by_mult":current_by_mult
},sort_keys=True))
