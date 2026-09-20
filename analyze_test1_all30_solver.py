import json, math
import blofin_live_hourly as bot

STATE="blofin_live_state.json"
OUT="test1_all30_solver_result.json"
START=1789914169376
HOLD_MIN=60
FEE=0.0012

with open(STATE,"r",encoding="utf-8") as f:
    st=json.load(f)
END=int((st.get("test1_reversed") or {}).get("started_at_ms") or 10**18)
trades=[t for t in st.get("trade_history",[]) if START <= int(t.get("opened_ms") or 0) < END][:30]

# Reuse already-computed entry-time features from previous analysis.
with open("test1_direction_solver_result.json","r",encoding="utf-8") as f:
    prev=json.load(f)
feat_by_n={int(r["n"]):r["features"] for r in prev["rows"]}

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

cache={}
for n,t in enumerate(trades,1):
    cache[n]=future_1m(t["inst"],int(t["opened_ms"]))

def sim(side,entry,mins,tp,sl):
    tpv=entry*(1+tp) if side=="LONG" else entry*(1-tp)
    slv=entry*(1-sl) if side=="LONG" else entry*(1+sl)
    for ts,o,h,l,c in mins:
        ht=(h>=tpv) if side=="LONG" else (l<=tpv)
        hs=(l<=slv) if side=="LONG" else (h>=slv)
        if ht and hs:
            return False,-sl-FEE,ts,"BOTH=>SL"
        if hs:return False,-sl-FEE,ts,"SL"
        if ht:return (tp-FEE)>0,tp-FEE,ts,"TP"
    if not mins:return False,-999,None,"NO_DATA"
    c=mins[-1][4]
    gross=(c/entry-1) if side=="LONG" else (entry/c-1)
    return gross-FEE>0,gross-FEE,mins[-1][0]+60000,"TIME"

TPS=[x/10000 for x in range(15,101,5)]  # 0.15%..1.00%
SLS=[x/10000 for x in [20,30,40,50,60,75,100,125,150,200,250,300]]

grid=[]
poss_by_pair={}
for tp in TPS:
  for sl in SLS:
    possible=0; labels={}
    margin=0.0
    for n,t in enumerate(trades,1):
        entry=float(t["open_price"]);mins=cache[n]
        lw,ln,_,_=sim("LONG",entry,mins,tp,sl)
        sw,sn,_,_=sim("SHORT",entry,mins,tp,sl)
        opts=[]
        if lw:opts.append(("LONG",ln))
        if sw:opts.append(("SHORT",sn))
        if opts:
            possible+=1
            # Default choose the side with larger net; later classifier can alter ties/both.
            labels[n]=max(opts,key=lambda x:x[1])[0]
            margin+=max(x[1] for x in opts)
        else:labels[n]=None
    grid.append((possible,margin,tp,sl,labels))
    poss_by_pair[(tp,sl)]=labels

grid.sort(key=lambda x:(x[0],x[1]),reverse=True)
best=grid[0]
all30=[x for x in grid if x[0]==30]
chosen=(sorted(all30,key=lambda x:(-x[2],x[3]))[0] if all30 else best)
possible,margin,tp,sl,labels=chosen

usable=[(n,t,feat_by_n[n],labels[n]) for n,t in enumerate(trades,1) if labels[n]]
names=sorted(k for k in usable[0][2] if all(u[2].get(k) is not None for u in usable))

# If both directions are profitable, allow either label to simplify the tree.
allowed={}
details={}
for n,t,fe,_ in usable:
    entry=float(t["open_price"]);mins=cache[n]
    lw,ln,_,lr=sim("LONG",entry,mins,tp,sl)
    sw,sn,_,sr=sim("SHORT",entry,mins,tp,sl)
    dirs=set()
    if lw:dirs.add("LONG")
    if sw:dirs.add("SHORT")
    allowed[n]=dirs
    details[n]={"long_win":lw,"long_net":ln,"short_win":sw,"short_net":sn}

# Build a tree where a leaf is valid if there is at least one direction allowed by every trade in that leaf.
def leaf_dir(ids):
    inter={"LONG","SHORT"}
    for ix in ids:inter &= allowed[usable[ix][0]]
    if inter:
        # Prefer majority original-volume-side-compatible not important; deterministic LONG then SHORT.
        return "LONG" if "LONG" in inter else "SHORT"
    return None

def impurity(ids):
    # 0 if one common profitable direction exists; else entropy of preferred labels.
    ld=leaf_dir(ids)
    if ld:return 0.0
    prefs=[]
    for ix in ids:
        n=usable[ix][0]
        d=details[n]
        prefs.append("LONG" if d["long_net"]>=d["short_net"] and d["long_win"] else "SHORT")
    a=sum(x=="LONG" for x in prefs);b=len(prefs)-a
    e=0
    for x in (a,b):
        if x:
            p=x/len(prefs);e-=p*math.log2(p)
    return e+1.0

def best_split(ids):
    base=impurity(ids);bestsp=None
    for name in names:
        vals=sorted(set(float(usable[i][2][name]) for i in ids))
        for a,b in zip(vals,vals[1:]):
            th=(a+b)/2
            le=[i for i in ids if usable[i][2][name]<=th]
            gt=[i for i in ids if usable[i][2][name]>th]
            if not le or not gt:continue
            score=base-(len(le)/len(ids))*impurity(le)-(len(gt)/len(ids))*impurity(gt)
            # Favor splits that create valid leaves.
            valid_bonus=(1 if leaf_dir(le) else 0)+(1 if leaf_dir(gt) else 0)
            key=(valid_bonus,score,-max(len(le),len(gt)))
            if bestsp is None or key>bestsp[0]:bestsp=(key,name,th,le,gt)
    return bestsp

def build(ids,depth=0,maxdepth=10):
    ld=leaf_dir(ids)
    if ld:
        return {"leaf":ld,"count":len(ids),"trade_numbers":[usable[i][0] for i in ids]}
    if depth>=maxdepth:
        return {"leaf":"IMPURE","count":len(ids),"trade_numbers":[usable[i][0] for i in ids]}
    sp=best_split(ids)
    if not sp:return {"leaf":"IMPURE","count":len(ids),"trade_numbers":[usable[i][0] for i in ids]}
    _,name,th,le,gt=sp
    return {"feature":name,"threshold":th,"le":build(le,depth+1,maxdepth),"gt":build(gt,depth+1,maxdepth)}

tree=build(list(range(len(usable))))

def pred(node,feat):
    while "leaf" not in node:
        node=node["le"] if feat[node["feature"]]<=node["threshold"] else node["gt"]
    return node["leaf"]

rows=[]
correct=0
for n,t,fe,_ in usable:
    p=pred(tree,fe)
    ok=p in allowed[n]
    correct+=ok
    rows.append({"n":n,"inst":t["inst"],"prediction":p,"profitable_directions":sorted(allowed[n]),"ok":ok,"detail":details[n]})

result={
 "search_horizon_minutes":HOLD_MIN,
 "fee_pct_roundtrip":FEE*100,
 "best_possible_count":best[0],
 "selected_tp_pct":tp*100,
 "selected_sl_pct":sl*100,
 "all30_pair_found":bool(all30),
 "tree_profitable_count":correct,
 "tree_total":len(usable),
 "tree":tree,
 "rows":rows,
 "top_grid":[{"possible":x[0],"tp_pct":x[2]*100,"sl_pct":x[3]*100} for x in grid[:20]]
}
with open(OUT,"w",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
print(json.dumps({k:result[k] for k in ["best_possible_count","selected_tp_pct","selected_sl_pct","all30_pair_found","tree_profitable_count","tree_total"]},ensure_ascii=False))
print(json.dumps(tree,ensure_ascii=False))
