"""READ ONLY: discover and independently check entry-only conditions appearing in
37 winning SHORT trades and 0 of 26 losing SHORT trades.
All rules use CLOSED 5-minute candles available by the moment the order opens.
No future candles, outcome or token identity in the conditions.
"""
from __future__ import annotations
from collections import Counter
from itertools import combinations
import json
import math
import statistics
from pathlib import Path

import short60_volume_mean10_24hour_readonly as core
from short60_subset_3to4x_winloss_explanation import features

# Do not mine columns that look into the future (holding_min, price_next5),
# and do not use coin ID or time-of-day to memorize outcomes.
SELECTORS={
    "vol_to_previous":"entry_volume_to_previous",
    "vol_to_avg10":"entry_volume_to_mean10",
    "red_candle_body_pct":"signal_red_body_pct",
    "red_body_to_range":"signal_body_fraction_of_range",
    "red_close_low_fraction":"signal_close_lower_range_fraction",
    "red_upper_wick":"upper_wick_fraction",
    "red_lower_wick":"lower_wick_fraction",
    "previous_green_body_pct":"previous_green_body_pct",
    "previous_six_red_count":"prior6_red_count",
    "change_last15_pct":"price_change_prior3_pct",
    "change_last30_pct":"price_change_prior6_pct",
    "change_last60_pct":"price_change_prior12_pct",
    "change_last150_pct":"price_change_prior30_pct",
    "rsi14_simple":"rsi14_simple",
    "sma10_gap_sma20_pct":"sma10_minus_sma20_pct",
    "sma10_slope_pct":"sma10_slope_pct",
    "rank_in_top200":"rank",
}
TRAIN_LIMIT=3
MIN_WIN=5

def bitselect(rows,feature,comparison,cutoff):
    mask=0
    for i,r in enumerate(rows):
        val=r.get(feature)
        if val is not None and math.isfinite(val):
            if (val>=cutoff if comparison==">=" else val<=cutoff):
                mask|=1<<i
    return mask

def clauses(rows):
    n=len(rows)
    winners=sum(1<<i for i,r in enumerate(rows) if r["outcome"]=="WIN")
    losers=((1<<n)-1)^winners
    out=[]
    unique_masks={}
    for label,field in SELECTORS.items():
        vals=sorted({float(r[field]) for r in rows if r.get(field) is not None and math.isfinite(r[field])})
        if not vals:continue
        # cuts are uniformly spaced percentiles from *training only*
        qs=(5,10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95)
        cuts=sorted({vals[min(len(vals)-1,round((len(vals)-1)*q/100))] for q in qs})
        for cutoff in cuts:
            for cmp in (">=","<="):
                mask=bitselect(rows,field,cmp,cutoff)
                w=(mask&winners).bit_count()
                l=(mask&losers).bit_count()
                if w<MIN_WIN or w==len(rows) or (w+l)==len(rows):
                    continue
                k=(label,mask)
                if k in unique_masks:continue
                unique_masks[k]=True
                out.append({"name":label,"key":field,"cmp":cmp,"threshold":cutoff,
                            "mask":mask,"train_wins":w,"train_losses":l})
    out.sort(key=lambda c:(c["name"],c["cmp"],c["threshold"]))
    return out,winners,losers

def cond_str(c):
    return f'{c["name"]} {c["cmp"]} {c["threshold"]:.6g}'

def test_conds(data,cls):
    s=[]
    for f in data:
        if all((f.get(c["key"]) is not None and
          ((f[c["key"]]>=c["threshold"]) if c["cmp"]==">=" else (f[c["key"]]<=c["threshold"])))
          for c in cls):
            s.append(f)
    c=Counter(x["outcome"] for x in s)
    rated=c["WIN"]+c["LOSS"]
    return {"wins":c["WIN"],"losses":c["LOSS"],"rated":rated,
            "win_pct":round(100*c["WIN"]/rated,2) if rated else None,
            "coin_count":len(set(x["coin"] for x in s)),
            "hours":len(set(x["entry_ms"]//3_600_000 for x in s))}

def main():
    data,errors,n=core.load()
    if errors or len(data)!=390:raise RuntimeError("Cannot verify all 390 coins")
    ranks,coverage=core.rankings(data)
    res=core.test_period(data,ranks,core.START,core.END,1.6)
    if (res["wins"],res["losses"],res["flat"])!=(528,649,21):
        raise RuntimeError("24h baseline counts changed unexpectedly")
    allfeats=[]
    for t in res["trades"]:
        if t["outcome"] not in ("WIN","LOSS"):continue
        f=features(t,data[t["inst"]])
        if f is None:continue
        allfeats.append(f)
    group=[x for x in allfeats if 3<=x["entry_volume_to_previous"]<4
           and x["price_change_prior30_pct"]>=0]
    wins=[r for r in group if r["outcome"]=="WIN"]
    losses=[r for r in group if r["outcome"]=="LOSS"]
    if (len(wins),len(losses))!=(37,26):
        raise RuntimeError(f"37/26 group mismatch: {(len(wins),len(losses))}")
    other=[x for x in allfeats if x not in group]
    assert len(other)==len(allfeats)-63
    selectors,W,L=clauses(group)
    print("SELECTORS="+str(len(selectors)),flush=True)
    candidate=[]

    def collect(indices,mask):
        w=(mask&W).bit_count()
        l=(mask&L).bit_count()
        if w>=MIN_WIN and l==0:
            found=[selectors[k] for k in indices]
            if len({c["name"] for c in found})<len(found):return
            candidate.append((w,indices))

    for i,s in enumerate(selectors):
        if s["train_losses"]==0:
            collect((i,),s["mask"])
    # Explore all 2-condition AND patterns
    for i,a in enumerate(selectors):
        for j in range(i+1,len(selectors)):
            b=selectors[j]
            if a["name"]==b["name"]:continue
            mask=a["mask"]&b["mask"]
            if (mask&W).bit_count()<MIN_WIN:continue
            collect((i,j),mask)
    # Explore 3-condition patterns only if first 2 retain at least MIN_WIN winners;
    # we limit third-stage branching to meaningful pairs to keep runtime bounded.
    triples=0
    for i,a in enumerate(selectors):
        for j in range(i+1,len(selectors)):
            b=selectors[j]
            if a["name"]==b["name"]:continue
            mask=a["mask"]&b["mask"]
            w=(mask&W).bit_count()
            if w<MIN_WIN:continue
            l=(mask&L).bit_count()
            if l==0:continue # already a 2-condition 100% pattern
            # If many losses remain, a third cutoff may still be useful.
            if l>12:continue
            for k in range(j+1,len(selectors)):
                c=selectors[k]
                if c["name"] in (a["name"],b["name"]):continue
                triple=mask&c["mask"]
                triples+=1
                if (triple&W).bit_count()<MIN_WIN or triple&L:continue
                collect((i,j,k),triple)
    print("TRIPLES_EVALUATED="+str(triples),flush=True)
    unique=set()
    output=[]
    for w,ids in sorted(candidate,key=lambda x:(-x[0],len(x[1]))):
        sel=[selectors[i] for i in ids]
        signature=tuple(sorted(cond_str(c) for c in sel))
        if signature in unique:continue
        unique.add(signature)
        # Evaluate on OTHER entries not used in winner-only discovery.
        out_sample=test_conds(other,sel)
        overall=test_conds(allfeats,sel)
        info={
           "conditions":[cond_str(c) for c in sel],
           "num_conditions":len(ids),
           "train_37_26":{"wins":w,"losses":0},
           "remaining_24h_1114_trades":out_sample,
           "all_24h_1177_trades":overall,
        }
        output.append(info)
        if len(output)>=120:break
    # Sort for best external performance (no cherry-picked claim, full set in report)
    output.sort(key=lambda x:(-x["train_37_26"]["wins"],
                    -x["remaining_24h_1114_trades"]["wins"],x["num_conditions"]))
    print("MINED_PATTERN_COUNT="+str(len(candidate)),flush=True)
    print("TOP_PATTERN_REPORT="+json.dumps({
      "group":{"wins":37,"losses":26},
      "others":{"wins":sum(x["outcome"]=="WIN" for x in other),
                "losses":sum(x["outcome"]=="LOSS" for x in other)},
      "selectors":len(selectors),"min_wins_required":MIN_WIN,
      "patterns_total":len(candidate),
      "patterns_top15":output[:15],
    },ensure_ascii=False,separators=(",",":")),flush=True)
    Path("short37_discriminating_volume_price_patterns_20261018.json").write_text(
     json.dumps({"group":{"wins":37,"losses":26},"selectors":len(selectors),
                 "all_pattern_results":output,
                 "qualifications":"Search uses multiple tests in training cohort, so 100% training is overfit until independent data validates. Controls only use pre-entry information. No future info. Non-target group in same 24h is additional data, not a future-day holdout."},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

if __name__=="__main__":main()
