import json, math, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot

DAYS=14
HOUR=3600000
MIN=60000
HOLD=5*HOUR
FEE=0.12
TPS=[0.4,0.5,0.6,0.7,0.8,1.0,1.2]
SLS=[0.3,0.4,0.5,0.6,0.75]
CURRENT_TP=0.6
CURRENT_SL=0.5

_,_,meta=bot.get_universe()
insts=sorted(meta)
print("UNIVERSE",len(insts))

def load1h(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":"450"})
        b=bot.decorate(bot.parse_candles(raw))
        return inst,b if len(b)>=80 else []
    except Exception as e:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=6) as ex:
    fs=[ex.submit(load1h,i) for i in insts]
    for n,f in enumerate(as_completed(fs),1):
        inst,b=f.result()
        if b: series[inst]=b
        if n%50==0: print("1H",n,len(series))

latest=max((b[-1]["ts"]+HOUR for b in series.values()),default=0)
end=(latest//HOUR)*HOUR
start=end-DAYS*24*HOUR
hours=list(range(start,end,HOUR))
maps={inst:{int(x["ts"]+HOUR):(k,x) for k,x in enumerate(b)} for inst,b in series.items()}
print("PERIOD",start,end,"HOURS",len(hours),"SERIES",len(series))

def sma(vals,n):
    out=[None]*len(vals)
    s=0.0
    for i,v in enumerate(vals):
        s+=v
        if i>=n: s-=vals[i-n]
        if i>=n-1: out[i]=s/n
    return out

def ema(vals,n):
    out=[None]*len(vals)
    if len(vals)<n:return out
    seed=sum(vals[:n])/n
    out[n-1]=seed
    a=2/(n+1)
    prev=seed
    for i in range(n,len(vals)):
        prev=a*vals[i]+(1-a)*prev
        out[i]=prev
    return out

def macd_state(b):
    c=[x["c"] for x in b]
    e12=ema(c,12); e26=ema(c,26)
    m=[None]*len(b)
    valid=[]
    idx=[]
    for i in range(len(b)):
        if e12[i] is not None and e26[i] is not None:
            m[i]=e12[i]-e26[i]; valid.append(m[i]); idx.append(i)
    sigv=ema(valid,9)
    s=[None]*len(b)
    for k,i in enumerate(idx): s[i]=sigv[k]
    out=[None]*len(b)
    for i in range(len(b)):
        if m[i] is not None and s[i] is not None:
            if m[i]>s[i]: out[i]="LONG"
            elif m[i]<s[i]: out[i]="SHORT"
    return out

def stoch_state(b):
    raw=[None]*len(b)
    for i in range(13,len(b)):
        hh=max(x["h"] for x in b[i-13:i+1]); ll=min(x["l"] for x in b[i-13:i+1])
        raw[i]=50.0 if hh==ll else 100*(b[i]["c"]-ll)/(hh-ll)
    k=[None]*len(b)
    for i in range(15,len(b)):
        xs=[raw[j] for j in range(i-2,i+1)]
        if all(x is not None for x in xs): k[i]=sum(xs)/3
    d=[None]*len(b)
    for i in range(17,len(b)):
        xs=[k[j] for j in range(i-2,i+1)]
        if all(x is not None for x in xs): d[i]=sum(xs)/3
    out=[None]*len(b)
    for i in range(len(b)):
        if k[i] is not None and d[i] is not None:
            if k[i]>d[i]: out[i]="LONG"
            elif k[i]<d[i]: out[i]="SHORT"
    return out

def adx_state(b,n=14):
    L=len(b)
    tr=[0.0]*L; pdm=[0.0]*L; mdm=[0.0]*L
    for i in range(1,L):
        up=b[i]["h"]-b[i-1]["h"]; dn=b[i-1]["l"]-b[i]["l"]
        pdm[i]=up if up>dn and up>0 else 0.0
        mdm[i]=dn if dn>up and dn>0 else 0.0
        tr[i]=max(b[i]["h"]-b[i]["l"],abs(b[i]["h"]-b[i-1]["c"]),abs(b[i]["l"]-b[i-1]["c"]))
    atr=[None]*L; sp=[None]*L; sm=[None]*L
    if L<=n:return [None]*L
    atr[n]=sum(tr[1:n+1]); sp[n]=sum(pdm[1:n+1]); sm[n]=sum(mdm[1:n+1])
    pdi=[None]*L; mdi=[None]*L; dx=[None]*L; adx=[None]*L
    for i in range(n,L):
        if i>n:
            atr[i]=atr[i-1]-atr[i-1]/n+tr[i]
            sp[i]=sp[i-1]-sp[i-1]/n+pdm[i]
            sm[i]=sm[i-1]-sm[i-1]/n+mdm[i]
        if atr[i] and atr[i]>0:
            pdi[i]=100*sp[i]/atr[i]; mdi[i]=100*sm[i]/atr[i]
            den=pdi[i]+mdi[i]
            dx[i]=0 if den==0 else 100*abs(pdi[i]-mdi[i])/den
    start=2*n-1
    if L>start:
        xs=[dx[i] for i in range(n,start+1) if dx[i] is not None]
        if len(xs)==n: adx[start]=sum(xs)/n
        for i in range(start+1,L):
            if adx[i-1] is not None and dx[i] is not None:
                adx[i]=(adx[i-1]*(n-1)+dx[i])/n
    out=[None]*L
    for i in range(L):
        if adx[i] is not None and adx[i]>=20 and pdi[i] is not None and mdi[i] is not None:
            if pdi[i]>mdi[i]: out[i]="LONG"
            elif mdi[i]>pdi[i]: out[i]="SHORT"
    return out

def supertrend_state(b,n=14,mult=2.0):
    L=len(b); tr=[0.0]*L
    for i in range(L):
        if i==0: tr[i]=b[i]["h"]-b[i]["l"]
        else: tr[i]=max(b[i]["h"]-b[i]["l"],abs(b[i]["h"]-b[i-1]["c"]),abs(b[i]["l"]-b[i-1]["c"]))
    atr=[None]*L
    if L<n:return [None]*L
    atr[n-1]=sum(tr[:n])/n
    for i in range(n,L): atr[i]=(atr[i-1]*(n-1)+tr[i])/n
    fu=[None]*L; fl=[None]*L; st=[None]*L; out=[None]*L
    for i in range(n-1,L):
        mid=(b[i]["h"]+b[i]["l"])/2
        bu=mid+mult*atr[i]; bl=mid-mult*atr[i]
        if i==n-1:
            fu[i]=bu; fl[i]=bl; st[i]=fu[i]
        else:
            fu[i]=bu if bu<fu[i-1] or b[i-1]["c"]>fu[i-1] else fu[i-1]
            fl[i]=bl if bl>fl[i-1] or b[i-1]["c"]<fl[i-1] else fl[i-1]
            if st[i-1]==fu[i-1]:
                st[i]=fu[i] if b[i]["c"]<=fu[i] else fl[i]
            else:
                st[i]=fl[i] if b[i]["c"]>=fl[i] else fu[i]
        out[i]="LONG" if b[i]["c"]>st[i] else "SHORT"
    return out

def obv_state(b):
    obv=[0.0]*len(b)
    for i in range(1,len(b)):
        if b[i]["c"]>b[i-1]["c"]: obv[i]=obv[i-1]+b[i]["v"]
        elif b[i]["c"]<b[i-1]["c"]: obv[i]=obv[i-1]-b[i]["v"]
        else: obv[i]=obv[i-1]
    out=[None]*len(b)
    for i in range(3,len(b)):
        if obv[i]>obv[i-3]:out[i]="LONG"
        elif obv[i]<obv[i-3]:out[i]="SHORT"
    return out

def dc20_state(b):
    out=[None]*len(b)
    for i in range(20,len(b)):
        hi=max(x["h"] for x in b[i-20:i]); lo=min(x["l"] for x in b[i-20:i])
        if b[i]["c"]>hi: out[i]="LONG"
        elif b[i]["c"]<lo: out[i]="SHORT"
    return out

states={}
for n,(inst,b) in enumerate(series.items(),1):
    c=[x["c"] for x in b]
    s20=sma(c,20); s50=sma(c,50)
    rsi=[None]*len(b)
    for i,x in enumerate(b):
        rv=x.get("rsi")
        if rv is not None:
            if rv>50:rsi[i]="LONG"
            elif rv<50:rsi[i]="SHORT"
    sma20=[None]*len(b); trend=[None]*len(b)
    for i in range(len(b)):
        if s20[i] is not None:
            sma20[i]="LONG" if c[i]>s20[i] else ("SHORT" if c[i]<s20[i] else None)
        if s20[i] is not None and s50[i] is not None:
            if c[i]>s20[i]>s50[i]:trend[i]="LONG"
            elif c[i]<s20[i]<s50[i]:trend[i]="SHORT"
    states[inst]={
      "RSI":rsi,"MACD":macd_state(b),"STOCH":stoch_state(b),
      "SMA20":sma20,"TREND":trend,"ADX":adx_state(b),
      "ST":supertrend_state(b),"OBV":obv_state(b),"DC20":dc20_state(b)
    }
    if n%50==0: print("IND",n)

COMBOS={
"VOL":[],
"VOL+RSI":["RSI"],
"VOL+MACD":["MACD"],
"VOL+STOCH":["STOCH"],
"VOL+SMA20":["SMA20"],
"VOL+SMA20+SMA50":["TREND"],
"VOL+ADX":["ADX"],
"VOL+SUPERTREND":["ST"],
"VOL+OBV":["OBV"],
"VOL+DC20":["DC20"],
"VOL+RSI+MACD":["RSI","MACD"],
"VOL+RSI+SMA20":["RSI","SMA20"],
"VOL+MACD+SMA20":["MACD","SMA20"],
"VOL+MACD+ADX":["MACD","ADX"],
"VOL+ADX+SMA20":["ADX","SMA20"],
"VOL+SUPERTREND+SMA20":["ST","SMA20"],
"VOL+MACD+SUPERTREND":["MACD","ST"],
"VOL+RSI+MACD+ADX":["RSI","MACD","ADX"],
"VOL+MACD+ADX+SUPERTREND":["MACD","ADX","ST"],
}

def volume_side(b,i):
    if i<1:return None
    cur=b[i]; cc=bot.candle_color(cur)
    if cc=="GREEN": target="RED"; side="LONG"
    elif cc=="RED": target="GREEN"; side="SHORT"
    else:return None
    for j in range(i-1,-1,-1):
        if bot.candle_color(b[j])==target:
            return side if cur["v"]>b[j]["v"] else None
    return None

hour_candidates={name:{} for name in COMBOS}
base_events=set()
for ix,t in enumerate(hours,1):
    prev=t-24*HOUR
    ranked=[]
    for inst,b in series.items():
        cp=maps[inst].get(t); pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0:continue
        ranked.append((cp[1]["c"]/pp[1]["c"]-1,inst,cp[0]))
    ranked.sort(reverse=True)
    top=ranked[:10]
    for name,reqs in COMBOS.items():
        cs=[]
        for rank,(_,inst,i) in enumerate(top,1):
            side=volume_side(series[inst],i)
            if not side:continue
            ok=True
            for req in reqs:
                if states[inst][req][i]!=side:
                    ok=False;break
            if ok:
                cs.append((rank,inst,side,t))
                base_events.add((inst,side,t))
        hour_candidates[name][t]=cs
    if ix%100==0:print("SCAN",ix,"EVENTS",len(base_events))
print("BASE_EVENTS",len(base_events))

cache={}
def load1m(ev):
    inst,side,t=ev
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1m","after":str(t+HOLD+MIN),"limit":"380"})
        a=[]
        for r in raw:
            try:
                if len(r)>=9 and str(r[8])=="1":
                    ts=int(r[0])
                    if t<=ts<=t+HOLD:a.append((ts,float(r[1]),float(r[2]),float(r[3]),float(r[4])))
            except:pass
        a.sort(); return ev,a
    except Exception:return ev,[]

with ThreadPoolExecutor(max_workers=4) as ex:
    fs=[ex.submit(load1m,e) for e in base_events]
    for n,f in enumerate(as_completed(fs),1):
        ev,a=f.result();cache[ev]=a
        if n%100==0:print("1M",n,len(base_events))

def outcome(ev,tp,sl):
    inst,side,t=ev;a=cache.get(ev,[])
    if not a:return None
    entry=a[0][1]
    tpv=entry*(1+tp/100) if side=="LONG" else entry*(1-tp/100)
    slv=entry*(1-sl/100) if side=="LONG" else entry*(1+sl/100)
    for ts,o,h,l,c in a:
        ht=h>=tpv if side=="LONG" else l<=tpv
        hs=l<=slv if side=="LONG" else h>=slv
        if ht and hs:return -sl,ts,True
        if hs:return -sl,ts,False
        if ht:return tp,ts,False
    ts,o,h,l,c=a[-1]
    p=((c/entry)-1)*100 if side=="LONG" else ((entry/c)-1)*100
    return p,ts+MIN,False

def simulate(name,tp,sl):
    until=0;tr=[]
    for t in hours:
        if t<until:continue
        cs=hour_candidates[name].get(t) or []
        if not cs:continue
        rank,inst,side,tt=cs[0]
        o=outcome((inst,side,tt),tp,sl)
        if not o:continue
        gross,exit_ts,amb=o
        tr.append((gross-FEE,gross,amb))
        until=exit_ts
    n=len(tr);wins=sum(x[0]>0 for x in tr);losses=sum(x[0]<0 for x in tr)
    net=sum(x[0] for x in tr);gross=sum(x[1] for x in tr);amb=sum(x[2] for x in tr)
    return {"combo":name,"tp":tp,"sl":sl,"trades":n,"wins":wins,"losses":losses,
      "ambiguous":amb,"win_rate":round(100*wins/n,2) if n else 0,
      "gross_pct":round(gross,3),"net_pct":round(net,3),
      "avg_net_pct":round(net/n,4) if n else 0}

summary=[]
for name in COMBOS:
    cur=simulate(name,CURRENT_TP,CURRENT_SL)
    grid=[simulate(name,tp,sl) for tp in TPS for sl in SLS]
    grid.sort(key=lambda x:(x["net_pct"],x["avg_net_pct"],x["trades"]),reverse=True)
    best=grid[0]
    row={"combo":name,"current":cur,"best":best}
    summary.append(row)
    print("COMBO "+json.dumps(row,sort_keys=True))

by_current=sorted([x["current"] for x in summary],key=lambda x:(x["net_pct"],x["trades"]),reverse=True)
by_best=sorted([x["best"] for x in summary],key=lambda x:(x["net_pct"],x["trades"]),reverse=True)
print("RANK_CURRENT "+json.dumps(by_current,sort_keys=True))
print("RANK_BEST "+json.dumps(by_best,sort_keys=True))
print("SUMMARY "+json.dumps({
 "days":DAYS,"hours":len(hours),"series":len(series),"base_events":len(base_events),
 "positive_current":sum(x["net_pct"]>0 for x in by_current),
 "positive_best":sum(x["net_pct"]>0 for x in by_best),
 "best_current":by_current[0] if by_current else None,
 "best_optimized":by_best[0] if by_best else None
},sort_keys=True))
