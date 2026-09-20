import json, math
import blofin_live_hourly as bot

STEP=5*60*1000
OUT="winner37_commonalities_result.json"

with open("test1_all30_solver_result.json","r",encoding="utf-8") as f: all30=json.load(f)
with open("test1_perfect_filter_result.json","r",encoding="utf-8") as f: base=json.load(f)
with open("winner_commonalities_result.json","r",encoding="utf-8") as f: common=json.load(f)
with open("random_hour2_common34_result.json","r",encoding="utf-8") as f: hour2=json.load(f)

pred={int(r["n"]):r["prediction"] for r in all30["rows"]}

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
    g=[];l=[]
    for i in range(1,n+1):
        d=vals[i]-vals[i-1];g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g)/n;al=sum(l)/n
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n+1,len(vals)):
        d=vals[i]-vals[i-1]
        ag=(ag*(n-1)+max(d,0))/n;al=(al*(n-1)+max(-d,0))/n
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
    st=2*n-1
    vals=[dx[i] for i in range(n,st+1) if dx[i] is not None]
    if len(vals)==n:aa[st]=sum(vals)/n
    for i in range(st+1,L):
        if aa[i-1] is not None and dx[i] is not None:aa[i]=(aa[i-1]*(n-1)+dx[i])/n
    return aa,pdi,mdi,atr

def features(inst, entry_ms):
    close_ms=(int(entry_ms)//STEP)*STEP
    raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"5m","after":str(close_ms+STEP),"limit":"240"})
    b=sorted([x for x in bot.parse_candles(raw) if int(x["ts"])+STEP<=close_ms],key=lambda x:x["ts"])
    i=len(b)-1;c=[x["c"] for x in b];v=[x["v"] for x in b]
    e9=ema(c,9);e21=ema(c,21);e12=ema(c,12);e26=ema(c,26);rs=rsi(c);K,D=stoch(b);A,P,M,ATR=adx(b)
    m=[None]*len(b);valid=[];idx=[]
    for k in range(len(b)):
        if e12[k] is not None and e26[k] is not None:
            m[k]=e12[k]-e26[k];valid.append(m[k]);idx.append(k)
    sv=ema(valid,9);sig=[None]*len(b)
    for kk,ix in enumerate(idx):
        if kk<len(sv):sig[ix]=sv[kk]
    obv=[0.0]*len(b)
    for k in range(1,len(b)):
        obv[k]=obv[k-1]+(v[k] if c[k]>c[k-1] else (-v[k] if c[k]<c[k-1] else 0))
    gv=rv=None
    for k in range(i,-1,-1):
        if gv is None and b[k]["c"]>b[k]["o"]:gv=b[k]["v"]
        if rv is None and b[k]["c"]<b[k]["o"]:rv=b[k]["v"]
        if gv is not None and rv is not None:break
    volavg=sum(v[max(0,i-19):i+1])/min(20,i+1)
    return {
      "rsi":rs[i],"macd_hist":m[i]-sig[i] if m[i] is not None and sig[i] is not None else None,
      "stoch_diff":K[i]-D[i] if K[i] is not None and D[i] is not None else None,
      "adx":A[i],"di_diff":P[i]-M[i] if P[i] is not None and M[i] is not None else None,
      "ema_gap":e9[i]/e21[i]-1 if e9[i] is not None and e21[i] is not None else None,
      "ret1":c[i]/c[i-1]-1,"ret3":c[i]/c[i-3]-1,"obv3":obv[i]-obv[i-3],
      "vol_ratio_gr":gv/rv if gv and rv else None,"vol_vs_avg":v[i]/volavg if volavg else None,
      "atr_pct":ATR[i]/14/c[i] if ATR[i] is not None and c[i] else None,
      "candle_body_pct":abs(b[i]["c"]-b[i]["o"])/b[i]["o"] if b[i]["o"] else None,
    }

def aligned(feat,side):
    sign=1 if side=="LONG" else -1
    return {
      "ema_aligned":sign*feat["ema_gap"],
      "rsi_aligned":sign*(feat["rsi"]-50),
      "macd_aligned":sign*feat["macd_hist"],
      "stoch_aligned":sign*feat["stoch_diff"],
      "di_aligned":sign*feat["di_diff"],
      "ret1_aligned":sign*feat["ret1"],
      "ret3_aligned":sign*feat["ret3"],
      "obv3_aligned":sign*feat["obv3"],
      "vol_ratio_aligned":feat["vol_ratio_gr"] if side=="LONG" else 1/feat["vol_ratio_gr"],
      "adx":feat["adx"],"vol_vs_avg":feat["vol_vs_avg"],"atr_pct":feat["atr_pct"],
      "candle_body_pct":feat["candle_body_pct"]
    }

wins=[]
for r in base["rows"]:
    side=pred[int(r["n"])]
    wins.append({"group":"TEST1_30","id":r["n"],"inst":r["inst"],"side":side,"f":aligned(r["features"],side)})
for r in common["random_rows"]:
    if r["win"]:
        wins.append({"group":"RANDOM1_4","id":r["n"],"inst":r["inst"],"side":r["side"],"f":aligned(r["features"],r["side"])})

new3=[]
for idx,r in enumerate(hour2["history"],1):
    if r["result"]!="WIN":continue
    f=features(r["inst"],r["entry_ms"])
    row={"group":"RANDOM2_3","id":idx,"inst":r["inst"],"side":r["side"],"f":aligned(f,r["side"])}
    wins.append(row);new3.append(row)

losses=[]
for idx,r in enumerate(hour2["history"],1):
    if r["result"]=="WIN":continue
    f=features(r["inst"],r["entry_ms"])
    losses.append({"group":"RANDOM2_LOSS","id":idx,"inst":r["inst"],"side":r["side"],"f":aligned(f,r["side"])})

names=list(wins[0]["f"].keys())
summary=[]
for name in names:
    w=[float(r["f"][name]) for r in wins if r["f"].get(name) is not None]
    l=[float(r["f"][name]) for r in losses if r["f"].get(name) is not None]
    lo=min(w);hi=max(w)
    summary.append({"feature":name,"min":lo,"max":hi,"mean":sum(w)/len(w),
                    "new3":[r["f"][name] for r in new3],
                    "losses_inside":sum(lo<=x<=hi for x in l),"losses_total":len(l)})
summary.sort(key=lambda x:(x["losses_inside"], x["feature"]))

# Find strongest simple all-37 common bounds.
conds=[]
for name in names:
    w=[float(r["f"][name]) for r in wins if r["f"].get(name) is not None]
    l=[float(r["f"][name]) for r in losses if r["f"].get(name) is not None]
    lo=min(w);hi=max(w)
    conds.append({"feature":name,"op":">=","threshold":lo,"losses_passing":sum(x>=lo for x in l)})
    conds.append({"feature":name,"op":"<=","threshold":hi,"losses_passing":sum(x<=hi for x in l)})
conds.sort(key=lambda x:(x["losses_passing"],x["feature"],x["op"]))

result={"winner_count":len(wins),"new_winners":new3,"summary":summary,"top_conditions":conds[:12]}
with open(OUT,"w",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
print("RESULT",json.dumps(result,ensure_ascii=False))
