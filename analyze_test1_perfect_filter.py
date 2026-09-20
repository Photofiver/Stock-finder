import json, math
import blofin_live_hourly as bot

STATE="blofin_live_state.json"
OUT="test1_perfect_filter_result.json"
START=1789914169376

with open(STATE,"r",encoding="utf-8") as f:
    st=json.load(f)
END=int((st.get("test1_reversed") or {}).get("started_at_ms") or 10**18)
trades=[t for t in st.get("trade_history",[]) if START <= int(t.get("opened_ms") or 0) < END][:30]

def ema(vals,n):
    out=[None]*len(vals)
    if len(vals)<n:return out
    seed=sum(vals[:n])/n
    out[n-1]=seed
    a=2/(n+1)
    p=seed
    for i in range(n,len(vals)):
        p=a*vals[i]+(1-a)*p
        out[i]=p
    return out

def rsi(vals,n=14):
    out=[None]*len(vals)
    if len(vals)<=n:return out
    gains=[];losses=[]
    for i in range(1,n+1):
        d=vals[i]-vals[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n+1,len(vals)):
        d=vals[i]-vals[i-1]
        ag=(ag*(n-1)+max(d,0))/n
        al=(al*(n-1)+max(-d,0))/n
        out[i]=100 if al==0 else 100-100/(1+ag/al)
    return out

def stoch(b,n=14):
    K=[None]*len(b);D=[None]*len(b)
    raw=[None]*len(b)
    for i in range(n-1,len(b)):
        hh=max(x["h"] for x in b[i-n+1:i+1]); ll=min(x["l"] for x in b[i-n+1:i+1])
        raw[i]=50 if hh==ll else 100*(b[i]["c"]-ll)/(hh-ll)
    for i in range(n+1,len(b)):
        xs=raw[i-2:i+1]
        if all(x is not None for x in xs):K[i]=sum(xs)/3
    for i in range(n+3,len(b)):
        xs=K[i-2:i+1]
        if all(x is not None for x in xs):D[i]=sum(xs)/3
    return K,D

def adx(b,n=14):
    L=len(b); tr=[0.0]*L; pdm=[0.0]*L; mdm=[0.0]*L
    for i in range(1,L):
        up=b[i]["h"]-b[i-1]["h"]; dn=b[i-1]["l"]-b[i]["l"]
        pdm[i]=up if up>dn and up>0 else 0.0
        mdm[i]=dn if dn>up and dn>0 else 0.0
        tr[i]=max(b[i]["h"]-b[i]["l"],abs(b[i]["h"]-b[i-1]["c"]),abs(b[i]["l"]-b[i-1]["c"]))
    atr=[None]*L;sp=[None]*L;sm=[None]*L;pdi=[None]*L;mdi=[None]*L;dx=[None]*L;aa=[None]*L
    if L<=2*n:return aa,pdi,mdi,atr
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
    start=2*n-1
    vals=[dx[i] for i in range(n,start+1) if dx[i] is not None]
    if len(vals)==n:aa[start]=sum(vals)/n
    for i in range(start+1,L):
        if aa[i-1] is not None and dx[i] is not None:
            aa[i]=(aa[i-1]*(n-1)+dx[i])/n
    return aa,pdi,mdi,atr

def features(inst, opened_ms, side):
    close_ms=(opened_ms//300000)*300000
    raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"5m","after":str(close_ms+300000),"limit":"240"})
    b=bot.parse_candles(raw)
    b=[x for x in b if int(x["ts"])+300000<=close_ms]
    b=sorted(b,key=lambda x:x["ts"])
    if len(b)<60:return None
    i=len(b)-1;c=[x["c"] for x in b];v=[x["v"] for x in b]
    e9=ema(c,9);e21=ema(c,21);e12=ema(c,12);e26=ema(c,26)
    m=[None]*len(b);vv=[];ii=[]
    for k in range(len(b)):
        if e12[k] is not None and e26[k] is not None:
            m[k]=e12[k]-e26[k];vv.append(m[k]);ii.append(k)
    sigv=ema(vv,9);sig=[None]*len(b)
    for kk,idx in enumerate(ii):
        if kk<len(sigv):sig[idx]=sigv[kk]
    rs=rsi(c,14);K,D=stoch(b,14);A,P,M,ATR=adx(b,14)
    obv=[0.0]*len(b)
    for k in range(1,len(b)):
        if c[k]>c[k-1]:obv[k]=obv[k-1]+v[k]
        elif c[k]<c[k-1]:obv[k]=obv[k-1]-v[k]
        else:obv[k]=obv[k-1]
    gv,rv=bot.last_green_red_volume(b,i)
    sign=1 if side=="LONG" else -1
    ret1=(c[i]/c[i-1]-1) if c[i-1] else 0
    ret3=(c[i]/c[i-3]-1) if c[i-3] else 0
    volavg=sum(v[max(0,i-19):i+1])/min(20,i+1)
    atrpct=(ATR[i]/14/c[i]) if ATR[i] is not None and c[i] else None
    feats={
      "rsi":rs[i],
      "rsi_aligned":sign*((rs[i] or 50)-50),
      "macd_hist":None if m[i] is None or sig[i] is None else m[i]-sig[i],
      "macd_aligned":None if m[i] is None or sig[i] is None else sign*(m[i]-sig[i]),
      "stoch_diff":None if K[i] is None or D[i] is None else K[i]-D[i],
      "stoch_aligned":None if K[i] is None or D[i] is None else sign*(K[i]-D[i]),
      "adx":A[i],
      "di_diff":None if P[i] is None or M[i] is None else P[i]-M[i],
      "di_aligned":None if P[i] is None or M[i] is None else sign*(P[i]-M[i]),
      "ema_gap":None if e9[i] is None or e21[i] is None else (e9[i]/e21[i]-1),
      "ema_aligned":None if e9[i] is None or e21[i] is None else sign*(e9[i]/e21[i]-1),
      "ret1":ret1,
      "ret1_aligned":sign*ret1,
      "ret3":ret3,
      "ret3_aligned":sign*ret3,
      "obv3":obv[i]-obv[i-3],
      "obv3_aligned":sign*(obv[i]-obv[i-3]),
      "vol_ratio_gr":None if not gv or not rv else gv/rv,
      "vol_ratio_aligned":None if not gv or not rv else (gv/rv if side=="LONG" else rv/gv),
      "vol_vs_avg":v[i]/volavg if volavg else None,
      "atr_pct":atrpct,
      "candle_body_pct":abs(b[i]["c"]-b[i]["o"])/b[i]["o"] if b[i]["o"] else None,
    }
    return feats

rows=[]
for n,t in enumerate(trades,1):
    f=features(t["inst"],int(t["opened_ms"]),t["side"])
    rows.append({
      "n":n,"inst":t["inst"],"side":t["side"],"net":float(t.get("net_pnl_usdt") or 0),
      "win":float(t.get("net_pnl_usdt") or 0)>0,"features":f
    })
    print("ROW",n,t["inst"],t["side"],"WIN" if rows[-1]["win"] else "LOSS")

feature_names=sorted({k for r in rows if r["features"] for k,v in r["features"].items() if v is not None})

conditions=[]
for name in feature_names:
    vals=sorted(set(float(r["features"][name]) for r in rows if r["features"] and r["features"].get(name) is not None))
    if not vals:continue
    thresholds=vals[:]
    for a,b in zip(vals,vals[1:]):thresholds.append((a+b)/2)
    for th in thresholds:
        for op in ("<=",">="):
            mask=[]
            for idx,r in enumerate(rows):
                x=None if not r["features"] else r["features"].get(name)
                ok=x is not None and (x<=th if op=="<=" else x>=th)
                if ok:mask.append(idx)
            if mask:
                w=sum(rows[i]["win"] for i in mask);l=len(mask)-w
                conditions.append({"feature":name,"op":op,"th":th,"mask":set(mask),"wins":w,"losses":l})

perfect1=[c for c in conditions if c["losses"]==0]
perfect1.sort(key=lambda c:(c["wins"],-abs(c["th"])),reverse=True)

best2=None
# Restrict to stronger conditions to keep combinatorics reasonable.
cand=sorted(conditions,key=lambda c:(c["wins"]-3*c["losses"],c["wins"]),reverse=True)[:1200]
for ai,a in enumerate(cand):
    for b in cand[ai+1:]:
        m=a["mask"] & b["mask"]
        if not m:continue
        w=sum(rows[i]["win"] for i in m);l=len(m)-w
        if l==0:
            cur=(w,a,b,m)
            if best2 is None or w>best2[0]:
                best2=cur

best={
 "single":None if not perfect1 else {k:v for k,v in perfect1[0].items() if k!="mask"},
 "pair":None
}
if best2:
    w,a,b,m=best2
    best["pair"]={
      "wins":w,"losses":0,
      "rule1":{k:v for k,v in a.items() if k not in ("mask","wins","losses")},
      "rule2":{k:v for k,v in b.items() if k not in ("mask","wins","losses")},
      "trade_numbers":[rows[i]["n"] for i in sorted(m)]
    }

result={
 "trades":len(rows),
 "wins_total":sum(r["win"] for r in rows),
 "losses_total":sum(not r["win"] for r in rows),
 "features":feature_names,
 "best_zero_loss_rules":best,
 "rows":rows
}
with open(OUT,"w",encoding="utf-8") as f:
    json.dump(result,f,ensure_ascii=False,indent=2)
print("RESULT",json.dumps(best,ensure_ascii=False))
