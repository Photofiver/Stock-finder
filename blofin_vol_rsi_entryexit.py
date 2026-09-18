import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot

DAYS=7; HOUR=3600000; MIN=60000; HOLD=5*HOUR
TP=0.6; SL=0.5; FEE=0.12

_,_,meta=bot.get_universe(); insts=sorted(meta)

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
end=(latest//HOUR)*HOUR; start=end-DAYS*24*HOUR
hours=list(range(start,end,HOUR))
maps={inst:{int(x["ts"]+HOUR):(k,x) for k,x in enumerate(b)} for inst,b in series.items()}

def rsi_state(b,i):
    rv=b[i].get("rsi")
    if rv is None:return None
    if rv>50:return "LONG"
    if rv<50:return "SHORT"
    return None

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

hc={}; events=set()
for t in hours:
    prev=t-24*HOUR; ranked=[]
    for inst,b in series.items():
        cp=maps[inst].get(t); pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0:continue
        ranked.append((cp[1]["c"]/pp[1]["c"]-1,inst,cp[0]))
    ranked.sort(reverse=True)
    cs=[]
    for rank,(_,inst,i) in enumerate(ranked[:10],1):
        side=volside(series[inst],i)
        if side and rsi_state(series[inst],i)==side:
            cs.append((rank,inst,side,t)); events.add((inst,side,t))
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

def opposite(side): return "SHORT" if side=="LONG" else "LONG"

def outcome(ev):
    inst,side,t=ev
    a=cache.get(ev,[])
    if not a:return None
    entry=a[0][1]
    tpv=entry*(1+TP/100) if side=="LONG" else entry*(1-TP/100)
    slv=entry*(1-SL/100) if side=="LONG" else entry*(1+SL/100)

    for ts,o,h,l,c in a:
        # RSI can only be acted on after a completed 1H candle.
        if ts>t and ts%HOUR==0:
            pair=maps[inst].get(ts)
            if pair and rsi_state(series[inst],pair[0])==opposite(side):
                gross=((o/entry)-1)*100 if side=="LONG" else ((entry/o)-1)*100
                return gross,ts,"RSI_FLIP",False

        hit_tp=h>=tpv if side=="LONG" else l<=tpv
        hit_sl=l<=slv if side=="LONG" else h>=slv
        if hit_tp and hit_sl:return -SL,ts,"AMBIG_SL",True
        if hit_sl:return -SL,ts,"SL",False
        if hit_tp:return TP,ts,"TP",False

    ts,o,h,l,c=a[-1]
    gross=((c/entry)-1)*100 if side=="LONG" else ((entry/c)-1)*100
    return gross,ts+MIN,"MAX5H",False

until=0; trades=[]
for t in hours:
    if t<until:continue
    cs=hc.get(t) or []
    if not cs:continue
    rank,inst,side,tt=cs[0]
    out=outcome((inst,side,tt))
    if not out:continue
    gross,ex,reason,amb=out
    trades.append({"t":tt,"inst":inst,"side":side,"rank":rank,"gross":gross,"net":gross-FEE,"exit":ex,"reason":reason,"ambiguous":amb,"hold_min":round((ex-tt)/60000,1)})
    until=ex

n=len(trades); wins=sum(x["net"]>0 for x in trades); losses=sum(x["net"]<0 for x in trades)
net=sum(x["net"] for x in trades); gross=sum(x["gross"] for x in trades)
reasons={}
for x in trades:reasons[x["reason"]]=reasons.get(x["reason"],0)+1
print("SUMMARY "+json.dumps({
  "combo":"VOL+RSI entry / RSI 50 flip exit","days":DAYS,"hours":len(hours),
  "trades":n,"wins":wins,"losses":losses,"win_rate":round(100*wins/n,2) if n else 0,
  "gross_pct":round(gross,3),"net_pct":round(net,3),"avg_net_pct":round(net/n,4) if n else 0,
  "avg_hold_min":round(sum(x["hold_min"] for x in trades)/n,1) if n else 0,
  "reasons":reasons,"ambiguous":sum(x["ambiguous"] for x in trades)
},sort_keys=True))
