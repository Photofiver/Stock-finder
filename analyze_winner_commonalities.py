import json, math
import blofin_live_hourly as bot

TEST1="test1_perfect_filter_result.json"
RANDOM="random_hour_tree_backtest_result.json"
OUT="winner_commonalities_result.json"
STEP=5*60*1000

with open(TEST1,"r",encoding="utf-8") as f:
    t1=json.load(f)
with open(RANDOM,"r",encoding="utf-8") as f:
    rnd=json.load(f)

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
            den=pdi[i]+mdi[i];dx[i]=0 if den==0 else 100*abs(pdi[i]-mdi[i])/den
    st=2*n-1
    vals=[dx[i] for i in range(n,st+1) if dx[i] is not None]
    if len(vals)==n:aa[st]=sum(vals)/n
    for i in range(st+1,L):
        if aa[i-1] is not None and dx[i] is not None:
            aa[i]=(aa[i-1]*(n-1)+dx[i])/n
    return aa,pdi,mdi,atr

def features(inst, entry_ms, side):
    close_ms=(int(entry_ms)//STEP)*STEP
    raw=bot.market_get("/api/v1/market/candles",{
        "instId":inst,"bar":"5m","after":str(close_ms+STEP),"limit":"240"
    })
    b=sorted([x for x in bot.parse_candles(raw) if int(x["ts"])+STEP<=close_ms],key=lambda x:x["ts"])
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
    sign=1 if side=="LONG" else -1
    return {
      "rsi":rs[i],
      "rsi_aligned":sign*((rs[i] or 50)-50),
      "macd_hist":None if m[i] is None or sig[i] is None else m[i]-sig[i],
      "macd_aligned":None if m[i] is None or sig[i] is None else sign*(m[i]-sig[i]),
      "stoch_diff":None if K[i] is None or D[i] is None else K[i]-D[i],
      "stoch_aligned":None if K[i] is None or D[i] is None else sign*(K[i]-D[i]),
      "adx":A[i],
      "di_diff":None if P[i] is None or M[i] is None else P[i]-M[i],
      "di_aligned":None if P[i] is None or M[i] is None else sign*(P[i]-M[i]),
      "ema_gap":None if e9[i] is None or e21[i] is None else e9[i]/e21[i]-1,
      "ema_aligned":None if e9[i] is None or e21[i] is None else sign*(e9[i]/e21[i]-1),
      "ret1":c[i]/c[i-1]-1,
      "ret1_aligned":sign*(c[i]/c[i-1]-1),
      "ret3":c[i]/c[i-3]-1,
      "ret3_aligned":sign*(c[i]/c[i-3]-1),
      "obv3":obv[i]-obv[i-3],
      "obv3_aligned":sign*(obv[i]-obv[i-3]),
      "vol_ratio_gr":None if not gv or not rv else gv/rv,
      "vol_ratio_aligned":None if not gv or not rv else (gv/rv if side=="LONG" else rv/gv),
      "vol_vs_avg":v[i]/volavg if volavg else None,
      "atr_pct":None if ATR[i] is None else ATR[i]/14/c[i],
      "candle_body_pct":abs(b[i]["c"]-b[i]["o"])/b[i]["o"] if b[i]["o"] else None,
    }

# TEST1 rows already contain entry-time features
t1_rows=[]
for r in t1["rows"]:
    t1_rows.append({
      "source":"TEST1","n":r["n"],"inst":r["inst"],"side":r["side"],
      "win":bool(r["win"]),"features":r["features"]
    })

rnd_rows=[]
for n,r in enumerate(rnd["history"],1):
    f=features(r["inst"],int(r["entry_ms"]),r["side"])
    rnd_rows.append({
      "source":"RANDOM","n":n,"inst":r["inst"],"side":r["side"],
      "win":r["result"]=="WIN","features":f,"result":r["result"],"pnl_usdt":r["pnl_usdt"]
    })
    print("RANDOM",n,r["inst"],r["side"],r["result"])

allrows=t1_rows+rnd_rows
wins=[r for r in allrows if r["win"] and r["features"]]
losses=[r for r in allrows if not r["win"] and r["features"]]
features=sorted(set.intersection(*[set(r["features"]) for r in wins]))

summary=[]
for name in features:
    wv=[float(r["features"][name]) for r in wins if r["features"].get(name) is not None]
    lv=[float(r["features"][name]) for r in losses if r["features"].get(name) is not None]
    if not wv: continue
    lo=min(wv);hi=max(wv)
    in_range=sum(lo<=x<=hi for x in lv)
    pos=sum(x>0 for x in wv);neg=sum(x<0 for x in wv);zero=len(wv)-pos-neg
    summary.append({
      "feature":name,"winner_min":lo,"winner_max":hi,"winner_mean":sum(wv)/len(wv),
      "winner_positive":pos,"winner_negative":neg,"winner_zero":zero,
      "losses_inside_winner_range":in_range,"losses_total":len(lv)
    })

# Search simple one-sided conditions satisfied by ALL winners; rank by fewest losses admitted.
conditions=[]
for name in features:
    wv=[float(r["features"][name]) for r in wins if r["features"].get(name) is not None]
    lv=[float(r["features"][name]) for r in losses if r["features"].get(name) is not None]
    lo=min(wv);hi=max(wv)
    conditions.append({"feature":name,"op":">=","threshold":lo,"winner_coverage":len(wv),
                       "losses_passing":sum(x>=lo for x in lv),"losses_total":len(lv)})
    conditions.append({"feature":name,"op":"<=","threshold":hi,"winner_coverage":len(wv),
                       "losses_passing":sum(x<=hi for x in lv),"losses_total":len(lv)})
conditions.sort(key=lambda x:(x["losses_passing"],x["feature"],x["op"]))

# Pairs of all-winner conditions that still cover all winners.
best_pairs=[]
for i,a in enumerate(conditions):
    for b in conditions[i+1:]:
        wp=0;lp=0
        for r in wins:
            fa=r["features"].get(a["feature"]);fb=r["features"].get(b["feature"])
            if fa is None or fb is None:continue
            oka=(fa>=a["threshold"] if a["op"]==">=" else fa<=a["threshold"])
            okb=(fb>=b["threshold"] if b["op"]==">=" else fb<=b["threshold"])
            if oka and okb:wp+=1
        if wp!=len(wins):continue
        for r in losses:
            fa=r["features"].get(a["feature"]);fb=r["features"].get(b["feature"])
            if fa is None or fb is None:continue
            oka=(fa>=a["threshold"] if a["op"]==">=" else fa<=a["threshold"])
            okb=(fb>=b["threshold"] if b["op"]==">=" else fb<=b["threshold"])
            if oka and okb:lp+=1
        best_pairs.append({"a":a,"b":b,"winner_coverage":wp,"losses_passing":lp})
best_pairs.sort(key=lambda x:x["losses_passing"])

result={
 "test1_winners":sum(r["win"] for r in t1_rows),
 "random_winners":sum(r["win"] for r in rnd_rows),
 "combined_winners":len(wins),
 "combined_losses":len(losses),
 "top_all_winner_conditions":conditions[:15],
 "top_all_winner_pairs":best_pairs[:10],
 "feature_summary":summary,
 "random_rows":rnd_rows,
}
with open(OUT,"w",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
print("SUMMARY",json.dumps({
  "test1_winners":result["test1_winners"],
  "random_winners":result["random_winners"],
  "combined_winners":result["combined_winners"],
  "combined_losses":result["combined_losses"],
  "top_conditions":result["top_all_winner_conditions"][:8],
  "top_pairs":result["top_all_winner_pairs"][:5],
},ensure_ascii=False))
