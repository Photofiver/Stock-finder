import json, math, time
import blofin_live_hourly as bot

STATE="blofin_live_state.json"
OUT="test1_direction_solver_result.json"
START=1789914169376
HOLD_MIN=60
TP=0.01
SL=0.005
ROUNDTRIP_FEE=0.0012  # 0.12%

with open(STATE,"r",encoding="utf-8") as f:
    st=json.load(f)
END=int((st.get("test1_reversed") or {}).get("started_at_ms") or 10**18)
trades=[t for t in st.get("trade_history",[]) if START <= int(t.get("opened_ms") or 0) < END][:30]

def ema(vals,n):
    out=[None]*len(vals)
    if len(vals)<n:return out
    seed=sum(vals[:n])/n; out[n-1]=seed
    a=2/(n+1); p=seed
    for i in range(n,len(vals)):
        p=a*vals[i]+(1-a)*p; out[i]=p
    return out

def rsi(vals,n=14):
    out=[None]*len(vals)
    if len(vals)<=n:return out
    g=[];l=[]
    for i in range(1,n+1):
        d=vals[i]-vals[i-1];g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g)/n;al=sum(l)/n
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n+1,len(vals)):
        d=vals[i]-vals[i-1]
        ag=(ag*(n-1)+max(d,0))/n; al=(al*(n-1)+max(-d,0))/n
        out[i]=100 if al==0 else 100-100/(1+ag/al)
    return out

def stoch(b,n=14):
    raw=[None]*len(b);K=[None]*len(b);D=[None]*len(b)
    for i in range(n-1,len(b)):
        hh=max(x["h"] for x in b[i-n+1:i+1]);ll=min(x["l"] for x in b[i-n+1:i+1])
        raw[i]=50 if hh==ll else 100*(b[i]["c"]-ll)/(hh-ll)
    for i in range(n+1,len(b)):
        xs=raw[i-2:i+1]
        if all(x is not None for x in xs):K[i]=sum(xs)/3
    for i in range(n+3,len(b)):
        xs=K[i-2:i+1]
        if all(x is not None for x in xs):D[i]=sum(xs)/3
    return K,D

def adx(b,n=14):
    L=len(b);tr=[0.0]*L;pdm=[0.0]*L;mdm=[0.0]*L
    for i in range(1,L):
        up=b[i]["h"]-b[i-1]["h"];dn=b[i-1]["l"]-b[i]["l"]
        pdm[i]=up if up>dn and up>0 else 0.0;mdm[i]=dn if dn>up and dn>0 else 0.0
        tr[i]=max(b[i]["h"]-b[i]["l"],abs(b[i]["h"]-b[i-1]["c"]),abs(b[i]["l"]-b[i-1]["c"]))
    atr=[None]*L;sp=[None]*L;sm=[None]*L;pdi=[None]*L;mdi=[None]*L;dx=[None]*L;aa=[None]*L
    if L<=2*n:return aa,pdi,mdi,atr
    atr[n]=sum(tr[1:n+1]);sp[n]=sum(pdm[1:n+1]);sm[n]=sum(mdm[1:n+1])
    for i in range(n,L):
        if i>n:
            atr[i]=atr[i-1]-atr[i-1]/n+tr[i];sp[i]=sp[i-1]-sp[i-1]/n+pdm[i];sm[i]=sm[i-1]-sm[i-1]/n+mdm[i]
        if atr[i] and atr[i]>0:
            pdi[i]=100*sp[i]/atr[i];mdi[i]=100*sm[i]/atr[i]
            den=pdi[i]+mdi[i];dx[i]=0 if den==0 else 100*abs(pdi[i]-mdi[i])/den
    start=2*n-1
    vals=[dx[i] for i in range(n,start+1) if dx[i] is not None]
    if len(vals)==n:aa[start]=sum(vals)/n
    for i in range(start+1,L):
        if aa[i-1] is not None and dx[i] is not None:aa[i]=(aa[i-1]*(n-1)+dx[i])/n
    return aa,pdi,mdi,atr

def entry_features(inst, opened_ms):
    close_ms=(opened_ms//300000)*300000
    raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"5m","after":str(close_ms+300000),"limit":"240"})
    b=sorted([x for x in bot.parse_candles(raw) if int(x["ts"])+300000<=close_ms],key=lambda x:x["ts"])
    if len(b)<60:return None
    i=len(b)-1;c=[x["c"] for x in b];v=[x["v"] for x in b]
    e9=ema(c,9);e21=ema(c,21);e12=ema(c,12);e26=ema(c,26);rs=rsi(c)
    m=[None]*len(b);valid=[];idx=[]
    for k in range(len(b)):
        if e12[k] is not None and e26[k] is not None:
            m[k]=e12[k]-e26[k];valid.append(m[k]);idx.append(k)
    sv=ema(valid,9);sig=[None]*len(b)
    for kk,ix in enumerate(idx):
        if kk<len(sv):sig[ix]=sv[kk]
    K,D=stoch(b);A,P,M,ATR=adx(b)
    obv=[0.0]*len(b)
    for k in range(1,len(b)):
        obv[k]=obv[k-1]+(v[k] if c[k]>c[k-1] else (-v[k] if c[k]<c[k-1] else 0))
    gv,rv=bot.last_green_red_volume(b,i)
    volavg=sum(v[max(0,i-19):i+1])/min(20,i+1)
    feats={
      "rsi":rs[i],
      "macd_hist":None if m[i] is None or sig[i] is None else m[i]-sig[i],
      "stoch_diff":None if K[i] is None or D[i] is None else K[i]-D[i],
      "adx":A[i],
      "di_diff":None if P[i] is None or M[i] is None else P[i]-M[i],
      "ema_gap":None if e9[i] is None or e21[i] is None else e9[i]/e21[i]-1,
      "ret1":c[i]/c[i-1]-1,
      "ret3":c[i]/c[i-3]-1,
      "obv3":obv[i]-obv[i-3],
      "vol_ratio_gr":None if not gv or not rv else gv/rv,
      "vol_vs_avg":v[i]/volavg if volavg else None,
      "atr_pct":None if ATR[i] is None else ATR[i]/14/c[i],
      "candle_body_pct":abs(b[i]["c"]-b[i]["o"])/b[i]["o"] if b[i]["o"] else None,
      "candle_dir":1 if b[i]["c"]>b[i]["o"] else (-1 if b[i]["c"]<b[i]["o"] else 0),
    }
    return feats

def future_1m(inst, opened_ms):
    start=(opened_ms//60000)*60000
    end=start+(HOLD_MIN+2)*60000
    raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1m","after":str(end),"limit":str(HOLD_MIN+10)})
    rows=[]
    for r in raw:
        if len(r)>=9 and str(r[8])=="1":
            ts=int(r[0])
            if start<=ts<=end:
                rows.append((ts,float(r[1]),float(r[2]),float(r[3]),float(r[4])))
    return sorted(rows)

def sim(side,entry,mins):
    tp=entry*(1+TP) if side=="LONG" else entry*(1-TP)
    sl=entry*(1-SL) if side=="LONG" else entry*(1+SL)
    for ts,o,h,l,c in mins:
        hit_tp=(h>=tp) if side=="LONG" else (l<=tp)
        hit_sl=(l<=sl) if side=="LONG" else (h>=sl)
        if hit_tp and hit_sl:
            # Conservative: count ambiguous bar as SL.
            gross=-SL
            return {"result":"LOSS","gross":gross,"net":gross-ROUNDTRIP_FEE,"exit_ms":ts,"why":"BOTH=>SL"}
        if hit_sl:
            gross=-SL
            return {"result":"LOSS","gross":gross,"net":gross-ROUNDTRIP_FEE,"exit_ms":ts,"why":"SL"}
        if hit_tp:
            gross=TP
            return {"result":"WIN","gross":gross,"net":gross-ROUNDTRIP_FEE,"exit_ms":ts,"why":"TP"}
    if not mins:return {"result":"NO_DATA","gross":None,"net":None,"exit_ms":None,"why":"NO_DATA"}
    c=mins[-1][4]
    gross=(c/entry-1) if side=="LONG" else (entry/c-1)
    net=gross-ROUNDTRIP_FEE
    return {"result":"WIN" if net>0 else "LOSS","gross":gross,"net":net,"exit_ms":mins[-1][0]+60000,"why":"TIME"}

rows=[]
for n,t in enumerate(trades,1):
    inst=t["inst"];opened=int(t["opened_ms"]);entry=float(t["open_price"])
    feats=entry_features(inst,opened);mins=future_1m(inst,opened)
    lo=sim("LONG",entry,mins);sh=sim("SHORT",entry,mins)
    # Pick a profitable direction. If both win, choose higher net. If neither, mark impossible under fixed management.
    choices=[("LONG",lo),("SHORT",sh)]
    wins=[x for x in choices if x[1]["result"]=="WIN"]
    desired=max(wins,key=lambda x:x[1]["net"])[0] if wins else None
    rows.append({"n":n,"inst":inst,"opened_ms":opened,"entry":entry,"original_side":t["side"],"features":feats,"long":lo,"short":sh,"desired":desired})
    print(n,inst,"LONG",lo["result"],round(lo["net"] or 0,6),"SHORT",sh["result"],round(sh["net"] or 0,6),"DESIRED",desired)

# Find a small decision tree from entry-time features that reproduces desired directions.
usable=[r for r in rows if r["desired"] in ("LONG","SHORT") and r["features"]]
names=sorted(k for k in usable[0]["features"] if all(r["features"].get(k) is not None for r in usable))

def entropy(ids):
    if not ids:return 0
    a=sum(usable[i]["desired"]=="LONG" for i in ids);b=len(ids)-a
    out=0
    for x in (a,b):
        if x:
            p=x/len(ids);out-=p*math.log2(p)
    return out

def best_split(ids,available_names):
    base=entropy(ids);best=None
    for name in available_names:
        vals=sorted(set(float(usable[i]["features"][name]) for i in ids))
        ths=[(a+b)/2 for a,b in zip(vals,vals[1:])]
        for th in ths:
            left=[i for i in ids if usable[i]["features"][name]<=th]
            right=[i for i in ids if usable[i]["features"][name]>th]
            if not left or not right:continue
            score=base-(len(left)/len(ids))*entropy(left)-(len(right)/len(ids))*entropy(right)
            if best is None or score>best[0]:best=(score,name,th,left,right)
    return best

def build(ids,depth=0,maxdepth=8):
    labels={usable[i]["desired"] for i in ids}
    if len(labels)==1:return {"leaf":next(iter(labels)),"count":len(ids),"trade_numbers":[usable[i]["n"] for i in ids]}
    if depth>=maxdepth:
        maj=max(labels,key=lambda l:sum(usable[i]["desired"]==l for i in ids))
        return {"leaf":maj,"count":len(ids),"impure":True,"trade_numbers":[usable[i]["n"] for i in ids]}
    sp=best_split(ids,names)
    if not sp:
        return {"leaf":"LONG","count":len(ids),"impure":True,"trade_numbers":[usable[i]["n"] for i in ids]}
    _,name,th,left,right=sp
    return {"feature":name,"threshold":th,"le":build(left,depth+1,maxdepth),"gt":build(right,depth+1,maxdepth)}

tree=build(list(range(len(usable))),maxdepth=8)

def predict(tree,feat):
    while "leaf" not in tree:
        tree=tree["le"] if feat[tree["feature"]]<=tree["threshold"] else tree["gt"]
    return tree["leaf"]

correct=sum(predict(tree,r["features"])==r["desired"] for r in usable)
result={
 "total":len(rows),
 "has_profitable_direction_fixed_tp_sl":sum(r["desired"] is not None for r in rows),
 "no_profitable_direction_fixed_tp_sl":[r["n"] for r in rows if r["desired"] is None],
 "decision_tree_correct":correct,
 "decision_tree_total":len(usable),
 "features_used":names,
 "tree":tree,
 "rows":rows,
}
with open(OUT,"w",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
print("SUMMARY",json.dumps({k:result[k] for k in ("total","has_profitable_direction_fixed_tp_sl","no_profitable_direction_fixed_tp_sl","decision_tree_correct","decision_tree_total")},ensure_ascii=False))
print("TREE",json.dumps(tree,ensure_ascii=False))
