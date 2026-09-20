import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
import blofin_live_hourly as bot

OUT="random_hour3_common37_result.json"
UK=ZoneInfo("Europe/London")

# Druga niezależna godzina z ubiegłego tygodnia.
START_UK=datetime(2026,9,15,18,0,tzinfo=UK)
END_UK=datetime(2026,9,15,19,0,tzinfo=UK)
START=int(START_UK.timestamp()*1000)
END=int(END_UK.timestamp()*1000)

STEP=5*60*1000
SCANS=list(range(START+STEP,END+1,STEP))
LOOKBACK=24*60*60*1000
FEE=0.0012
TP=0.01
SL=0.01
MAX_POS=4
START_BANKROLL=10.0

# Wspólne zakresy 30 zwycięskich z TEST1 + 4 zwycięskich z pierwszej losowej godziny.
ADX_MIN=14.224453257513431
ADX_MAX=43.93076012880488
VOL_ALIGNED_MIN=0.045372520133965734
VOL_ALIGNED_MAX=28.025316455696206
EMA_ALIGNED_MIN=-0.04291420216380937
RSI_ALIGNED_MIN=-23.43946987418647

_,_,meta=bot.get_universe()
insts=sorted(meta)

def load5(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{
            "instId":inst,"bar":"5m","after":str(END+STEP),"limit":"330"
        })
        b=bot.parse_candles(raw)
        b=sorted([x for x in b if START-LOOKBACK-STEP <= int(x["ts"]) <= END],key=lambda x:x["ts"])
        return inst,b
    except Exception:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=12) as ex:
    for f in as_completed([ex.submit(load5,i) for i in insts]):
        inst,b=f.result()
        if b:series[inst]=b

maps={inst:{int(x["ts"]+STEP):idx for idx,x in enumerate(b)} for inst,b in series.items()}

def ema(vals,n):
    out=[None]*len(vals)
    if len(vals)<n:return out
    seed=sum(vals[:n])/n;out[n-1]=seed
    a=2/(n+1);p=seed
    for i in range(n,len(vals)):
        p=a*vals[i]+(1-a)*p;out[i]=p
    return out

def rsi(vals,n=14):
    out=[None]*len(vals)
    if len(vals)<=n:return out
    gains=[];losses=[]
    for i in range(1,n+1):
        d=vals[i]-vals[i-1]
        gains.append(max(d,0));losses.append(max(-d,0))
    ag=sum(gains)/n;al=sum(losses)/n
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n+1,len(vals)):
        d=vals[i]-vals[i-1]
        ag=(ag*(n-1)+max(d,0))/n
        al=(al*(n-1)+max(-d,0))/n
        out[i]=100 if al==0 else 100-100/(1+ag/al)
    return out

def adx_vals(b,n=14):
    L=len(b);tr=[0.0]*L;pdm=[0.0]*L;mdm=[0.0]*L
    for i in range(1,L):
        up=b[i]["h"]-b[i-1]["h"];dn=b[i-1]["l"]-b[i]["l"]
        pdm[i]=up if up>dn and up>0 else 0.0
        mdm[i]=dn if dn>up and dn>0 else 0.0
        tr[i]=max(b[i]["h"]-b[i]["l"],abs(b[i]["h"]-b[i-1]["c"]),abs(b[i]["l"]-b[i-1]["c"]))
    atr=[None]*L;sp=[None]*L;sm=[None]*L;pdi=[None]*L;mdi=[None]*L;dx=[None]*L;adx=[None]*L
    if L<=2*n:return adx
    atr[n]=sum(tr[1:n+1]);sp[n]=sum(pdm[1:n+1]);sm[n]=sum(mdm[1:n+1])
    for i in range(n,L):
        if i>n:
            atr[i]=atr[i-1]-atr[i-1]/n+tr[i]
            sp[i]=sp[i-1]-sp[i-1]/n+pdm[i]
            sm[i]=sm[i-1]-sm[i-1]/n+mdm[i]
        if atr[i] and atr[i]>0:
            pdi[i]=100*sp[i]/atr[i];mdi[i]=100*sm[i]/atr[i]
            den=pdi[i]+mdi[i]
            dx[i]=0 if den==0 else 100*abs(pdi[i]-mdi[i])/den
    st=2*n-1
    vals=[dx[i] for i in range(n,st+1) if dx[i] is not None]
    if len(vals)==n:adx[st]=sum(vals)/n
    for i in range(st+1,L):
        if adx[i-1] is not None and dx[i] is not None:
            adx[i]=(adx[i-1]*(n-1)+dx[i])/n
    return adx

ind={}
for inst,b in series.items():
    c=[x["c"] for x in b];v=[x["v"] for x in b]
    e9=ema(c,9);e21=ema(c,21);a=adx_vals(b);rs=rsi(c)
    vavg=[None]*len(b)
    for i in range(19,len(b)):vavg[i]=sum(v[i-19:i+1])/20
    ind[inst]=(e9,e21,a,rs,vavg)

def last_green_red_volume(b,i):
    gv=rv=None
    for k in range(i,-1,-1):
        if gv is None and b[k]["c"]>b[k]["o"]:gv=b[k]["v"]
        if rv is None and b[k]["c"]<b[k]["o"]:rv=b[k]["v"]
        if gv is not None and rv is not None:break
    return gv,rv

def frozen_direction(inst,i):
    b=series[inst];e9,e21,a,rs,vavg=ind[inst]
    if e9[i] is None or e21[i] is None or a[i] is None or vavg[i] in (None,0):return None
    gap=e9[i]/e21[i]-1
    volavg_ratio=b[i]["v"]/vavg[i]
    if gap <= -0.01595175251580222:return "SHORT"
    if a[i] > 39.39395023293147:return "SHORT"
    if gap > 0.010145004811231484:return "LONG"
    if gap > 0.0034414889057717835:return "SHORT"
    if volavg_ratio > 1.6967889596896342:return "SHORT"
    return "LONG"

def passes_common34(inst,i,side):
    b=series[inst];e9,e21,a,rs,vavg=ind[inst]
    if any(x is None for x in (e9[i],e21[i],a[i],rs[i])):return False
    sign=1 if side=="LONG" else -1
    ema_aligned=sign*(e9[i]/e21[i]-1)
    rsi_aligned=sign*(rs[i]-50)
    gv,rv=last_green_red_volume(b,i)
    if not gv or not rv:return False
    vol_aligned=(gv/rv) if side=="LONG" else (rv/gv)
    return (
        ADX_MIN <= a[i] <= ADX_MAX and
        VOL_ALIGNED_MIN <= vol_aligned <= VOL_ALIGNED_MAX and
        ema_aligned >= EMA_ALIGNED_MIN and
        rsi_aligned >= RSI_ALIGNED_MIN
    )

def top10_at(t):
    ranked=[];prev=t-LOOKBACK
    for inst,b in series.items():
        i=maps[inst].get(t);j=maps[inst].get(prev)
        if i is None or j is None:continue
        old=b[j]["c"];cur=b[i]["c"]
        if old and cur:ranked.append((cur/old-1,inst,i))
    ranked.sort(reverse=True)
    return ranked[:10]

tops={t:top10_at(t) for t in SCANS}
union=sorted({inst for t in SCANS for _,inst,_ in tops[t]})

def load1m(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{
            "instId":inst,"bar":"1m","after":str(END+61*60000),"limit":"140"
        })
        rows=[]
        for r in raw:
            if len(r)>=9 and str(r[8])=="1":
                ts=int(r[0])
                if START <= ts <= END+60*60000:
                    rows.append({"ts":ts,"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4])})
        return inst,sorted(rows,key=lambda x:x["ts"])
    except Exception:
        return inst,[]

m1={}
with ThreadPoolExecutor(max_workers=8) as ex:
    for f in as_completed([ex.submit(load1m,i) for i in union]):
        inst,b=f.result();m1[inst]=b
m1map={inst:{x["ts"]:x for x in rows} for inst,rows in m1.items()}

positions={};history=[];bankroll=START_BANKROLL;last_event=START

def close_pos(inst,exit_price,exit_ms,reason):
    global bankroll
    p=positions.pop(inst)
    gross=(exit_price/p["entry"]-1) if p["side"]=="LONG" else (p["entry"]/exit_price-1)
    net=gross-FEE;pnl=p["notional"]*net;bankroll+=pnl
    history.append({"inst":inst,"side":p["side"],"entry_ms":p["entry_ms"],"exit_ms":exit_ms,
                    "pnl_usdt":pnl,"result":"WIN" if pnl>0 else ("LOSS" if pnl<0 else "FLAT"),"reason":reason})

def process_interval(a,b):
    for ts in range(a,b,60000):
        for inst in list(positions):
            bar=m1map.get(inst,{}).get(ts)
            if not bar:continue
            p=positions.get(inst)
            if not p:continue
            tp=p["entry"]*(1+TP) if p["side"]=="LONG" else p["entry"]*(1-TP)
            sl=p["entry"]*(1-SL) if p["side"]=="LONG" else p["entry"]*(1+SL)
            ht=bar["h"]>=tp if p["side"]=="LONG" else bar["l"]<=tp
            hs=bar["l"]<=sl if p["side"]=="LONG" else bar["h"]>=sl
            if ht and hs:close_pos(inst,sl,ts,"BOTH=>SL")
            elif hs:close_pos(inst,sl,ts,"SL")
            elif ht:close_pos(inst,tp,ts,"TP")

scan_rows=[]
for t in SCANS:
    process_interval(last_event,t);last_event=t
    dirs=[]
    for rank,(_,inst,i) in enumerate(tops[t],1):
        side=frozen_direction(inst,i)
        if side and passes_common34(inst,i,side):
            dirs.append((rank,inst,side,i))

    for rank,inst,side,i in dirs:
        if inst in positions and positions[inst]["side"]!=side:
            close_pos(inst,series[inst][i]["c"],t,"OPPOSITE SIGNAL")

    slots=max(0,MAX_POS-len(positions));chosen=[]
    for rank,inst,side,i in dirs:
        if inst in positions:continue
        chosen.append((rank,inst,side,i))
        if len(chosen)>=slots:break

    if chosen:
        exposure=sum(p["notional"] for p in positions.values())
        remaining=max(0.0,bankroll-exposure)
        per=remaining/len(chosen)
        for rank,inst,side,i in chosen:
            if per<=0:break
            positions[inst]={"side":side,"entry":series[inst][i]["c"],"entry_ms":t,"notional":per,"rank":rank}

    scan_rows.append({"scan_ms":t,"signals":[{"rank":r,"inst":i,"side":s} for r,i,s,_ in dirs],
                      "opened":[{"rank":r,"inst":i,"side":s} for r,i,s,_ in chosen],
                      "open_count":len(positions),"bankroll":bankroll})

process_interval(last_event,END+60*60000)
for inst in list(positions):
    bars=m1.get(inst,[])
    px=next((x["c"] for x in reversed(bars) if x["ts"]<=END+60*60000),positions[inst]["entry"])
    close_pos(inst,px,END+60*60000,"60M SETTLEMENT")

wins=sum(x["result"]=="WIN" for x in history)
losses=sum(x["result"]=="LOSS" for x in history)
flat=sum(x["result"]=="FLAT" for x in history)
result={
 "period_uk":f"{START_UK.isoformat()} to {END_UK.isoformat()}",
 "scans":len(SCANS),
 "strategy":"Frozen TEST1 direction tree + common37 filters",
 "trades":len(history),"wins":wins,"losses":losses,"flat":flat,
 "win_rate_pct":100*wins/len(history) if history else 0,
 "starting_bankroll_usdt":START_BANKROLL,
 "ending_bankroll_usdt":bankroll,
 "net_pnl_usdt":sum(x["pnl_usdt"] for x in history),
 "history":history,"scan_rows":scan_rows
}
with open(OUT,"w",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
print("SUMMARY",json.dumps({k:result[k] for k in ["period_uk","scans","trades","wins","losses","flat","win_rate_pct","starting_bankroll_usdt","ending_bankroll_usdt","net_pnl_usdt"]},ensure_ascii=False))
