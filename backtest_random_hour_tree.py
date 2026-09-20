import json, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
import blofin_live_hourly as bot

OUT="random_hour_tree_backtest_result.json"
UK=ZoneInfo("Europe/London")
UTC=ZoneInfo("UTC")

# Fixed random hour chosen from last week: 17 Sep 2026, 13:00-14:00 UK.
START_UK=datetime(2026,9,17,13,0,tzinfo=UK)
END_UK=datetime(2026,9,17,14,0,tzinfo=UK)
START=int(START_UK.timestamp()*1000)
END=int(END_UK.timestamp()*1000)
STEP=5*60*1000
SCANS=list(range(START+STEP, END+1, STEP))
LOOKBACK=24*60*60*1000
FEE=0.0012
TP=0.01
SL=0.01
MAX_POS=4
START_BANKROLL=10.0

_,_,meta=bot.get_universe()
insts=sorted(meta)
print("INSTRUMENTS",len(insts))

# Get 5m history covering the 24h ranking lookback + the test hour.
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
    fs=[ex.submit(load5,i) for i in insts]
    for f in as_completed(fs):
        inst,b=f.result()
        if b: series[inst]=b
print("SERIES",len(series))

maps={inst:{int(x["ts"]+STEP):idx for idx,x in enumerate(b)} for inst,b in series.items()}

def ema(vals,n):
    out=[None]*len(vals)
    if len(vals)<n:return out
    seed=sum(vals[:n])/n;out[n-1]=seed
    a=2/(n+1);p=seed
    for i in range(n,len(vals)):
        p=a*vals[i]+(1-a)*p;out[i]=p
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
            den=pdi[i]+mdi[i];dx[i]=0 if den==0 else 100*abs(pdi[i]-mdi[i])/den
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
    e9=ema(c,9);e21=ema(c,21);adx=adx_vals(b)
    vavg=[None]*len(b)
    for i in range(len(b)):
        if i>=19:vavg[i]=sum(v[i-19:i+1])/20
    ind[inst]=(e9,e21,adx,vavg)

def direction(inst,i):
    b=series[inst];e9,e21,adx,vavg=ind[inst]
    if e9[i] is None or e21[i] is None or adx[i] is None or vavg[i] in (None,0):
        return None
    ema_gap=e9[i]/e21[i]-1
    vol_vs_avg=b[i]["v"]/vavg[i]

    # Frozen rule found from TEST 1. No future data used here.
    if ema_gap <= -0.01595175251580222:
        return "SHORT"
    if adx[i] > 39.39395023293147:
        return "SHORT"
    if ema_gap > 0.010145004811231484:
        return "LONG"
    if ema_gap > 0.0034414889057717835:
        return "SHORT"
    if vol_vs_avg > 1.6967889596896342:
        return "SHORT"
    return "LONG"

def top10_at(t):
    ranked=[]
    prev=t-LOOKBACK
    for inst,b in series.items():
        i=maps[inst].get(t);j=maps[inst].get(prev)
        if i is None or j is None:continue
        old=b[j]["c"];cur=b[i]["c"]
        if old and cur:
            ranked.append((cur/old-1,inst,i))
    ranked.sort(reverse=True)
    return ranked[:10]

tops={t:top10_at(t) for t in SCANS}
union=sorted({inst for t in SCANS for _,inst,_ in tops[t]})
print("TOP UNION",len(union),union)

# 1m bars for precise TP/SL and mark-to-market at scan boundaries.
def load1m(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{
            "instId":inst,"bar":"1m","after":str(END+60000),"limit":"80"
        })
        rows=[]
        for r in raw:
            if len(r)>=9 and str(r[8])=="1":
                ts=int(r[0])
                if START <= ts <= END:
                    rows.append({"ts":ts,"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4])})
        return inst,sorted(rows,key=lambda x:x["ts"])
    except Exception:
        return inst,[]

m1={}
with ThreadPoolExecutor(max_workers=8) as ex:
    fs=[ex.submit(load1m,i) for i in union]
    for f in as_completed(fs):
        inst,b=f.result();m1[inst]=b

positions={}
history=[]
bankroll=START_BANKROLL
last_event=START

def close_pos(inst,exit_price,exit_ms,reason):
    global bankroll
    p=positions.pop(inst)
    if p["side"]=="LONG":
        gross=exit_price/p["entry"]-1
    else:
        gross=p["entry"]/exit_price-1
    net=gross-FEE
    pnl=p["notional"]*net
    bankroll += pnl
    history.append({
      "inst":inst,"side":p["side"],"entry_ms":p["entry_ms"],"exit_ms":exit_ms,
      "entry":p["entry"],"exit":exit_price,"gross_pct":gross*100,
      "net_pct":net*100,"pnl_usdt":pnl,"result":"WIN" if pnl>0 else ("LOSS" if pnl<0 else "FLAT"),
      "reason":reason
    })

def process_interval(a,b):
    # Process minute bars [a,b) in chronological order for open positions.
    for ts in range(a,b,60000):
        for inst in list(positions):
            bar=next((x for x in m1.get(inst,[]) if x["ts"]==ts),None)
            if not bar:continue
            p=positions.get(inst)
            if not p:continue
            tp=p["entry"]*(1+TP) if p["side"]=="LONG" else p["entry"]*(1-TP)
            sl=p["entry"]*(1-SL) if p["side"]=="LONG" else p["entry"]*(1+SL)
            ht=bar["h"]>=tp if p["side"]=="LONG" else bar["l"]<=tp
            hs=bar["l"]<=sl if p["side"]=="LONG" else bar["h"]>=sl
            if ht and hs:
                close_pos(inst,sl,ts,"BOTH=>SL")
            elif hs:
                close_pos(inst,sl,ts,"SL")
            elif ht:
                close_pos(inst,tp,ts,"TP")

scan_rows=[]
for t in SCANS:
    process_interval(last_event,t)
    last_event=t

    ranked=tops[t]
    dirs=[]
    for rank,(_,inst,i) in enumerate(ranked,1):
        d=direction(inst,i)
        if d:dirs.append((rank,inst,d,i))

    # Same TEST1 behaviour: opposite direction at a later scan closes the tracked position first.
    for rank,inst,d,i in dirs:
        if inst in positions and positions[inst]["side"]!=d:
            px=series[inst][i]["c"]
            close_pos(inst,px,t,"OPPOSITE SIGNAL")

    slots=max(0,MAX_POS-len(positions))
    chosen=[]
    for rank,inst,d,i in dirs:
        if inst in positions:continue
        chosen.append((rank,inst,d,i))
        if len(chosen)>=slots:break

    if chosen:
        exposure=sum(p["notional"] for p in positions.values())
        remaining=max(0.0,bankroll-exposure)
        per=remaining/len(chosen) if chosen else 0
        for rank,inst,d,i in chosen:
            if per<=0:break
            entry=series[inst][i]["c"]
            positions[inst]={"side":d,"entry":entry,"entry_ms":t,"notional":per,"rank":rank}
    scan_rows.append({
      "scan_ms":t,
      "top10":[x[1] for x in ranked],
      "signals":[{"rank":r,"inst":i,"side":d} for r,i,d,_ in dirs],
      "opened":[{"rank":r,"inst":i,"side":d} for r,i,d,_ in chosen],
      "open_count":len(positions),
      "bankroll":bankroll,
    })

# Close remaining at end-of-hour market price, same as finalizing a one-hour test.
process_interval(last_event,END+60000)
for inst in list(positions):
    # Prefer 14:00 5m close if available.
    i=maps.get(inst,{}).get(END)
    px=series[inst][i]["c"] if i is not None else positions[inst]["entry"]
    close_pos(inst,px,END,"END OF TEST")

wins=sum(x["result"]=="WIN" for x in history)
losses=sum(x["result"]=="LOSS" for x in history)
flat=sum(x["result"]=="FLAT" for x in history)
netp=sum(x["pnl_usdt"] for x in history)
result={
 "period_uk":f"{START_UK.isoformat()} to {END_UK.isoformat()}",
 "scans":len(SCANS),
 "strategy":"Frozen TEST1 30/30 direction tree",
 "tp_pct":1.0,
 "sl_pct":1.0,
 "fee_roundtrip_pct":0.12,
 "max_open_positions":MAX_POS,
 "starting_bankroll_usdt":START_BANKROLL,
 "ending_bankroll_usdt":bankroll,
 "net_pnl_usdt":netp,
 "trades":len(history),
 "wins":wins,"losses":losses,"flat":flat,
 "win_rate_pct":(100*wins/len(history) if history else 0),
 "history":history,
 "scan_rows":scan_rows,
}
with open(OUT,"w",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
print("SUMMARY",json.dumps({k:result[k] for k in ["period_uk","scans","trades","wins","losses","flat","win_rate_pct","starting_bankroll_usdt","ending_bankroll_usdt","net_pnl_usdt"]},ensure_ascii=False))
