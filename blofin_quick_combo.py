import json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot

DAYS=7
HOUR=3600000
MIN=60000
HOLD=5*HOUR
FEE=0.12
TP=0.6
SL=0.5

_,_,meta=bot.get_universe()
insts=sorted(meta)

def load1h(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":"220"})
        b=bot.decorate(bot.parse_candles(raw))
        return inst,b if len(b)>=80 else []
    except:return inst,[]

series={}
with ThreadPoolExecutor(max_workers=8) as ex:
    for f in as_completed([ex.submit(load1h,i) for i in insts]):
        inst,b=f.result()
        if b:series[inst]=b

latest=max((b[-1]["ts"]+HOUR for b in series.values()),default=0)
end=(latest//HOUR)*HOUR
start=end-DAYS*24*HOUR
hours=list(range(start,end,HOUR))
maps={inst:{int(x["ts"]+HOUR):(k,x) for k,x in enumerate(b)} for inst,b in series.items()}

def sma(v,n):
    o=[None]*len(v);s=0
    for i,x in enumerate(v):
        s+=x
        if i>=n:s-=v[i-n]
        if i>=n-1:o[i]=s/n
    return o

def ema(v,n):
    o=[None]*len(v)
    if len(v)<n:return o
    p=sum(v[:n])/n;o[n-1]=p;a=2/(n+1)
    for i in range(n,len(v)):
        p=a*v[i]+(1-a)*p;o[i]=p
    return o

def macd(b):
    c=[x["c"] for x in b];a=ema(c,12);z=ema(c,26);m=[None]*len(b);vv=[];ii=[]
    for i in range(len(b)):
        if a[i] is not None and z[i] is not None:m[i]=a[i]-z[i];vv.append(m[i]);ii.append(i)
    ss=ema(vv,9);sig=[None]*len(b)
    for k,i in enumerate(ii):sig[i]=ss[k]
    o=[None]*len(b)
    for i in range(len(b)):
        if m[i] is not None and sig[i] is not None:o[i]="LONG" if m[i]>sig[i] else "SHORT"
    return o

def stoch(b):
    k=[None]*len(b);d=[None]*len(b);raw=[None]*len(b)
    for i in range(13,len(b)):
        hi=max(x["h"] for x in b[i-13:i+1]);lo=min(x["l"] for x in b[i-13:i+1])
        raw[i]=50 if hi==lo else 100*(b[i]["c"]-lo)/(hi-lo)
    for i in range(15,len(b)):
        xs=raw[i-2:i+1]
        if all(x is not None for x in xs):k[i]=sum(xs)/3
    for i in range(17,len(b)):
        xs=k[i-2:i+1]
        if all(x is not None for x in xs):d[i]=sum(xs)/3
    o=[None]*len(b)
    for i in range(len(b)):
        if k[i] is not None and d[i] is not None:o[i]="LONG" if k[i]>d[i] else "SHORT"
    return o

def adx(b,n=14):
    L=len(b);tr=[0]*L;pdm=[0]*L;mdm=[0]*L
    for i in range(1,L):
        up=b[i]["h"]-b[i-1]["h"];dn=b[i-1]["l"]-b[i]["l"]
        pdm[i]=up if up>dn and up>0 else 0;mdm[i]=dn if dn>up and dn>0 else 0
        tr[i]=max(b[i]["h"]-b[i]["l"],abs(b[i]["h"]-b[i-1]["c"]),abs(b[i]["l"]-b[i-1]["c"]))
    atr=[None]*L;sp=[None]*L;sm=[None]*L;pdi=[None]*L;mdi=[None]*L;dx=[None]*L;adxv=[None]*L
    if L<=n:return [None]*L
    atr[n]=sum(tr[1:n+1]);sp[n]=sum(pdm[1:n+1]);sm[n]=sum(mdm[1:n+1])
    for i in range(n,L):
        if i>n:
            atr[i]=atr[i-1]-atr[i-1]/n+tr[i];sp[i]=sp[i-1]-sp[i-1]/n+pdm[i];sm[i]=sm[i-1]-sm[i-1]/n+mdm[i]
        if atr[i]:
            pdi[i]=100*sp[i]/atr[i];mdi[i]=100*sm[i]/atr[i];den=pdi[i]+mdi[i];dx[i]=0 if not den else 100*abs(pdi[i]-mdi[i])/den
    st=2*n-1
    if L>st:
        xs=[dx[i] for i in range(n,st+1) if dx[i] is not None]
        if len(xs)==n:adxv[st]=sum(xs)/n
        for i in range(st+1,L):
            if adxv[i-1] is not None and dx[i] is not None:adxv[i]=(adxv[i-1]*(n-1)+dx[i])/n
    o=[None]*L
    for i in range(L):
        if adxv[i] is not None and adxv[i]>=20:o[i]="LONG" if pdi[i]>mdi[i] else "SHORT"
    return o

states={}
for inst,b in series.items():
    c=[x["c"] for x in b];s20=sma(c,20);s50=sma(c,50)
    rsi=[];sma20=[];trend=[]
    for i,x in enumerate(b):
        rv=x.get("rsi");rsi.append("LONG" if rv and rv>50 else ("SHORT" if rv is not None and rv<50 else None))
        sma20.append("LONG" if s20[i] is not None and c[i]>s20[i] else ("SHORT" if s20[i] is not None and c[i]<s20[i] else None))
        trend.append("LONG" if s20[i] is not None and s50[i] is not None and c[i]>s20[i]>s50[i] else ("SHORT" if s20[i] is not None and s50[i] is not None and c[i]<s20[i]<s50[i] else None))
    states[inst]={"RSI":rsi,"MACD":macd(b),"STOCH":stoch(b),"SMA20":sma20,"TREND":trend,"ADX":adx(b)}

COMBOS={
"VOL":[],
"VOL+RSI":["RSI"],
"VOL+MACD":["MACD"],
"VOL+STOCH":["STOCH"],
"VOL+SMA20":["SMA20"],
"VOL+EMA20/50":["TREND"],
"VOL+ADX":["ADX"],
"VOL+RSI+MACD":["RSI","MACD"],
"VOL+RSI+SMA20":["RSI","SMA20"],
"VOL+MACD+SMA20":["MACD","SMA20"],
"VOL+MACD+ADX":["MACD","ADX"],
"VOL+ADX+SMA20":["ADX","SMA20"],
"VOL+RSI+MACD+ADX":["RSI","MACD","ADX"],
}

def volside(b,i):
    if i<1:return None
    cur=b[i];cc=bot.candle_color(cur)
    if cc=="GREEN":tar="RED";side="LONG"
    elif cc=="RED":tar="GREEN";side="SHORT"
    else:return None
    for j in range(i-1,-1,-1):
        if bot.candle_color(b[j])==tar:return side if cur["v"]>b[j]["v"] else None
    return None

hc={k:{} for k in COMBOS};events=set()
for t in hours:
    prev=t-24*HOUR;rank=[]
    for inst,b in series.items():
        cp=maps[inst].get(t);pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0:continue
        rank.append((cp[1]["c"]/pp[1]["c"]-1,inst,cp[0]))
    rank.sort(reverse=True)
    top=rank[:10]
    for name,reqs in COMBOS.items():
        cs=[]
        for r,(_,inst,i) in enumerate(top,1):
            side=volside(series[inst],i)
            if side and all(states[inst][q][i]==side for q in reqs):
                cs.append((r,inst,side,t));events.add((inst,side,t))
        hc[name][t]=cs

cache={}
def one(ev):
    inst,side,t=ev
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1m","after":str(t+HOLD+MIN),"limit":"380"})
        a=[]
        for r in raw:
            if len(r)>=9 and str(r[8])=="1":
                ts=int(r[0])
                if t<=ts<=t+HOLD:a.append((ts,float(r[1]),float(r[2]),float(r[3]),float(r[4])))
        a.sort();return ev,a
    except:return ev,[]

with ThreadPoolExecutor(max_workers=12) as ex:
    for f in as_completed([ex.submit(one,e) for e in events]):
        ev,a=f.result();cache[ev]=a

def outcome(ev):
    inst,side,t=ev;a=cache.get(ev,[])
    if not a:return None
    e=a[0][1];tpv=e*(1+TP/100) if side=="LONG" else e*(1-TP/100);slv=e*(1-SL/100) if side=="LONG" else e*(1+SL/100)
    for ts,o,h,l,c in a:
        ht=h>=tpv if side=="LONG" else l<=tpv;hs=l<=slv if side=="LONG" else h>=slv
        if ht and hs:return -SL,ts,True
        if hs:return -SL,ts,False
        if ht:return TP,ts,False
    ts,o,h,l,c=a[-1];p=((c/e)-1)*100 if side=="LONG" else ((e/c)-1)*100
    return p,ts+MIN,False

rows=[]
for name in COMBOS:
    until=0;tr=[]
    for t in hours:
        if t<until:continue
        cs=hc[name].get(t) or []
        if not cs:continue
        r,inst,side,tt=cs[0];o=outcome((inst,side,tt))
        if not o:continue
        gross,ex,amb=o;tr.append((gross-FEE,gross,amb));until=ex
    n=len(tr);wins=sum(x[0]>0 for x in tr);losses=sum(x[0]<0 for x in tr);net=sum(x[0] for x in tr);gross=sum(x[1] for x in tr)
    rows.append({"combo":name,"trades":n,"wins":wins,"losses":losses,"win_rate":round(100*wins/n,2) if n else 0,"gross_pct":round(gross,3),"net_pct":round(net,3),"avg_net":round(net/n,4) if n else 0,"ambiguous":sum(x[2] for x in tr)})
rows.sort(key=lambda x:(x["net_pct"],x["avg_net"]),reverse=True)
print("RESULTS "+json.dumps(rows,sort_keys=True))
print("SUMMARY "+json.dumps({"days":DAYS,"hours":len(hours),"events":len(events),"best":rows[0] if rows else None,"positive":sum(x["net_pct"]>0 for x in rows)},sort_keys=True))
