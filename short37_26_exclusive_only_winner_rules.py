"""READ ONLY: STRICTLY the user's chosen 37 winning vs 26 losing SHORT trades.

Goal: find features/patterns that occur in winners but NOT in any of
those 26 losers. Search single features and 2/3 condition conjunctions,
and an interpretable decision tree. Source is the archived 390-coin
OHLCV, used only to reconstruct these exact 63 selected entries and
the 63 pre-entry features. NEVER train/check on other trades/days.
No private API, credentials, LIVE edits or orders.

CRITICAL: 100% purity applies ONLY to these historical 63 labels.
"""
from __future__ import annotations
from collections import Counter
import json, math, statistics, time
from pathlib import Path
import numpy as np
from sklearn.tree import DecisionTreeClassifier, _tree
import short60_volume_mean10_24hour_readonly as base
import short37_extra_indicators_readonly as xtra

MIN_WIN=4
WIN_COUNT=37
LOSS_COUNT=26
MASKALL=(1<<63)-1

def feature_universe():
    market,err,n=base.load()
    if err or n!=390 or len(market)!=390:raise RuntimeError("Invalid OHLCV source")
    ranks,_=base.rankings(market)
    res=base.test_period(market,ranks,base.START,base.END,1.6)
    if (res["wins"],res["losses"])!=(528,649):raise RuntimeError("24h dataset not reproduced")
    btc=next(k for k in market if k=="BTC-USDT")
    cand=[]
    for t in res["trades"]:
        if t["outcome"] not in ("WIN","LOSS"):continue
        if not (t["volume_ratio"] is not None and 3<=t["volume_ratio"]<4):continue
        f=xtra.features(t,market[t["inst"]],market[btc])
        if f is None:raise RuntimeError("Missing selected trade features")
        if f["price_prior150_pct"]>=0:
            cand.append(f)
    cnt=Counter(t["outcome"] for t in cand)
    if (cnt["WIN"],cnt["LOSS"])!=(37,26):
        raise RuntimeError("Selected cohort count mismatch "+str(cnt))
    # Stable, auditable order, winners first then losers.
    cand=sorted(cand,key=lambda t:(t["outcome"]!="WIN",t["entry_ms"],t["coin"]))
    cols=[z for z in cand[0] if z not in ("outcome","coin","entry_ms")]
    missing={k:sum(v[k] is None for v in cand) for k in cols}
    return cand,cols,missing

def build_thresholds(rows,keys):
    rules={}
    winners=(1<<37)-1
    losers=MASKALL^winners
    for key in keys:
        vals=sorted(set(float(r[key]) for r in rows if r[key] is not None))
        if len(vals)<2:continue
        # Test every possible threshold at every observed value.
        for threshold in vals:
            mask_lt=0;mask_gt=0
            for j,r in enumerate(rows):
                v=r.get(key)
                if v is None:continue
                if v<=threshold:mask_lt|=1<<j
                if v>=threshold:mask_gt|=1<<j
            for op,mask in (("<=",mask_lt),(">=",mask_gt)):
                w=(mask&winners).bit_count()
                l=(mask&losers).bit_count()
                if w<MIN_WIN or l==LOSS_COUNT:continue
                if mask==MASKALL:continue
                sig=(key,op,float(threshold))
                prev=rules.get(mask)
                # Keep the first one in case of equivalent masks.
                if prev is None:rules[mask]=sig
    # Keep all unique masks, remove masks identical across different columns.
    arr=[(m,feat) for m,feat in rules.items()]
    arr.sort(key=lambda item:(-(item[0]&winners).bit_count(),(item[0]&losers).bit_count(),item[1][0]))
    return arr,winners,losers

def desc(c):
    return f"{c[0]} {c[1]} {c[2]:.8g}"

def score(mask,winners,losers):
    return ((mask&winners).bit_count(),(mask&losers).bit_count())

def search(rows,keys):
    start=time.monotonic()
    ss,W,L=build_thresholds(rows,keys)
    print("RULES_UNIQUE="+str(len(ss)),flush=True)
    print("SOURCE_FEATURES="+str(len(keys)),flush=True)
    out=[]
    seen=set()
    def add(mask,cond):
        w,l=score(mask,W,L)
        if w<MIN_WIN or l:return
        name=tuple(desc(z) for z in cond)
        if name in seen:return
        seen.add(name)
        out.append({"num_conditions":len(cond),"wins":w,"losses":0,
                    "conditions":list(name),
                    "coins":[{"coin":rows[i]["coin"],"time_uk":base.clock(rows[i]["entry_ms"])}
                              for i in range(37) if mask&(1<<i)]})
    for mask,c in ss:
        if not mask&L:add(mask,(c,))
    # Exhaustive search all distinct two feature masks (bounded by number observed unique cuts).
    best_pairs={}
    exact_pairs=0
    total_pairs=0
    for i,(a,ca) in enumerate(ss):
        ka=ca[0]
        for b,cb in ss[i+1:]:
            if cb[0]==ka:continue
            total_pairs+=1
            together=a&b
            w,l=score(together,W,L)
            if w<MIN_WIN:continue
            if l==0:
                exact_pairs+=1
                add(together,(ca,cb))
            elif l<=10 and w>=MIN_WIN+4:
                # Search 3-rule refinements of useful 2-rule intersections.
                # Store max 1,250 masks, retaining the better winner/loss ratios.
                old=best_pairs.get(together)
                if old is None:best_pairs[together]=(ca,cb,w,l)
    print("PAIR_SEARCH="+json.dumps({"comparisons":total_pairs,
                     "zero_loss_pairs":exact_pairs,
                     "near_pure_unique_pair_masks":len(best_pairs)}),flush=True)
    # Beam: choose up to 3,000 non-pure pairs with highest win coverage and low losses.
    pairs=sorted(best_pairs.items(),key=lambda x:(-x[1][2]/x[1][3],-x[1][2],x[1][3]))[:3000]
    n3=0
    for mask, (ca,cb,w,l) in pairs:
        for other,c in ss:
            if c[0] in (ca[0],cb[0]):continue
            joined=mask&other
            wins,losses=score(joined,W,L)
            if wins<MIN_WIN:continue
            if losses==0:
                n3+=1
                add(joined,(ca,cb,c))
    result=sorted(out,key=lambda z:(-z["wins"],z["num_conditions"],z["conditions"]))
    # Group equivalent results by winners selected (do not repeat nearly identical rules).
    unique={}
    for o in result:
        signature=tuple((x["coin"],x["time_uk"]) for x in o["coins"])
        if signature not in unique:unique[signature]=o
    distinct=list(unique.values())
    # Greedy set cover: OR of easy zero-loser rules until all winners possible are covered.
    masks=[]
    lookup={(r["coin"],base.clock(r["entry_ms"])):i for i,r in enumerate(rows[:37])}
    for r in distinct:
        mask=0
        for coin in r["coins"]:
            mask|=1<<lookup[(coin["coin"],coin["time_uk"])]
        masks.append(mask)
    remaining=(1<<37)-1
    cover=[]
    for _ in range(10):
        choices=[((m&remaining).bit_count(),-r["num_conditions"],i) for i,(m,r) in enumerate(zip(masks,distinct))]
        if not choices:break
        n,_,i=max(choices)
        if not n:break
        cover.append(distinct[i])
        remaining &=~masks[i]
    return {
       "unique_threshold_masks":len(ss),"pair_comparisons":total_pairs,
       "pure_rules_count":len(out),"distinct_winner_subsets":len(distinct),
       "search_3rd_clause_near_pure_pairs":len(pairs),
       "pure_3_condition_rules_explored":n3,
       "top_15":distinct[:15],"top_wins":distinct[0]["wins"] if distinct else 0,
       "pure_rule_or_coverage":37-remaining.bit_count(),
       "pure_rule_or_rules_used":len(cover),
       "pure_rule_or_conditions":[r["conditions"] for r in cover],
       "elapsed_s":round(time.monotonic()-start,2),
    }

def trees(rows,keys):
    # Fit ONLY these 63 samples; no accuracy claims on other data.
    filtered=[k for k in keys if all(row.get(k) is not None for row in rows)]
    feats=np.array([[float(row[k]) for k in filtered] for row in rows])
    y=np.array([1 if x["outcome"]=="WIN" else 0 for x in rows])
    reports=[]
    for depth in (1,2,3,4,5,6,8,12,None):
        for minleaf in (3,2,1):
            model=DecisionTreeClassifier(max_depth=depth,min_samples_leaf=minleaf,
                  random_state=42,min_impurity_decrease=0.000001).fit(feats,y)
            pred=model.predict(feats)
            win_found=int(sum((pred==1)&(y==1)))
            losers_accepted=int(sum((pred==1)&(y==0)))
            numleaves=model.get_n_leaves()
            # Extract paths for pure positive leaves.
            tree=model.tree_
            def extract(node,path):
                if tree.feature[node]==_tree.TREE_UNDEFINED:
                    value=tree.value[node][0]
                    total=float(sum(value))
                    # Values may be normalized class probabilities
                    if value[1]>0 and value[0]==0:
                        mask=model.apply(feats)==node
                        subset=[rows[i] for i in np.flatnonzero(mask)]
                        return [{"conditions":path,"wins":sum(x["outcome"]=="WIN" for x in subset),
                                 "losses":sum(x["outcome"]=="LOSS" for x in subset)}]
                    return []
                key=filtered[tree.feature[node]]
                thr=tree.threshold[node]
                a=extract(tree.children_left[node],path+[f"{key} <= {thr:.8g}"])
                b=extract(tree.children_right[node],path+[f"{key} > {thr:.8g}"])
                return a+b
            leaves=extract(0,[])
            reports.append({"max_depth":depth if depth is not None else "unrestricted",
               "min_leaf_samples":minleaf,"leaves":numleaves,"wins_predicted":win_found,
               "losers_wrongly_accepted":losers_accepted,
               "pure_positive_paths":sorted(leaves,key=lambda x:-x["wins"])[:10]})
    reports.sort(key=lambda o:(o["losers_wrongly_accepted"],-o["wins_predicted"],
                               o["leaves"],(o["max_depth"] if isinstance(o["max_depth"],int) else 100)))
    return {"searched_models":len(reports),"top_models":reports[:15],
            "simplest_full_separation":next((r for r in sorted(reports,
                   key=lambda o:(o["leaves"],o["max_depth"] if isinstance(o["max_depth"],int) else 100))
                   if r["losers_wrongly_accepted"]==0 and r["wins_predicted"]==37),None)}

def main():
    rows,keys,missing=feature_universe()
    assert len(rows)==63
    print("CHOSEN_COHORT="+json.dumps({"winners":37,"losers":26,"features":len(keys),
                                      "missing":{k:v for k,v in missing.items() if v}}),flush=True)
    rules=search(rows,keys)
    print("RULE_DISCOVERY_SUMMARY="+json.dumps({k:v for k,v in rules.items() if k!="top_15"},
           ensure_ascii=False,separators=(",",":")),flush=True)
    print("TOP_PURE_RULES="+json.dumps(rules["top_15"][:7],ensure_ascii=False,separators=(",",":")),flush=True)
    tree=trees(rows,keys)
    print("DECISION_TREES="+json.dumps({"searched":tree["searched_models"],
             "top_models":tree["top_models"][:6],
             "simplest_full_separation":tree["simplest_full_separation"]},ensure_ascii=False,separators=(",",":")),flush=True)
    report={"subset":"ONLY 37 winning SHORT and 26 losing SHORT selected 2026-10-17 18:00 to 2026-10-18 18:00 UK, volume ratio 3..4, 2.5h previous price >=0, mean10 volume cap.",
       "features":keys,"missing":missing,"rules":rules,"trees":tree,
       "warning":"Pure training rules are IN-SAMPLE fitted after seeing outcomes; even exact 37/0 separation is not evidence of real-time predictability or live profitability. Exhaustive only for one- and two-condition rules; three-condition expansion is bounded to 3,000 promising pair masks. No other trades or dates inspected for condition performance. All technical inputs computed solely from preceding closed candles; no history leaks or LIVE changes."}
    p=Path("short37_26_exclusive_only_winner_rules_report.json")
    p.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("REPORT_WRITTEN="+str(p),flush=True)
if __name__=="__main__":main()
