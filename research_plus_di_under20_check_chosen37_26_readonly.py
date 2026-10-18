"""READ-ONLY independent cohort check of fixed plus-DI thresholds found from
the SEPARATE last-hour 3-short-wins vs 7-losses TOP10 sample.
No parameters optimized on 37/26, no orders, no LIVE edits.

Source original frozen 390 BloFin OHLCV shard artifacts. STRICT pre-entry
features only. Emphasize no independent days, still small and data selected.
"""
from __future__ import annotations
from collections import Counter
from pathlib import Path
import json,statistics
import short60_volume_mean10_24hour_readonly as base
from short37_extra_indicators_readonly import features

def summary(xs):
    c=Counter(x["outcome"] for x in xs)
    wc=c["WIN"]; lc=c["LOSS"]
    gross=sum(x["gross_pct"] for x in xs)
    return {"wins":wc,"losses":lc,"win_rate_pct":round(100*wc/(wc+lc),2) if wc+lc else None,
            "gross_sum_pct_points":round(gross,4),
            "estimated_fee_pct_points":round(.12*len(xs),4),
            "net_sum_pct_points":round(gross-.12*len(xs),4)}
def main():
    market,errors,total=base.load()
    if errors or len(market)!=390:raise RuntimeError("Incomplete frozen market shards")
    ranks,coverage=base.rankings(market)
    allresult=base.test_period(market,ranks,base.START,base.END,1.6)
    if (allresult["wins"],allresult["losses"])!=(528,649):raise RuntimeError("Changed base backtest")
    subset=[]
    for t in allresult["trades"]:
        if t["outcome"] not in ("WIN","LOSS"):continue
        if t["volume_ratio"] is None or not 3<=t["volume_ratio"]<4:continue
        f=features(t,market[t["inst"]],market["BTC-USDT"])
        if f is not None and f["price_prior150_pct"]>=0:
            subset.append({**f,**t})
    counts=Counter(x["outcome"] for x in subset)
    if (counts["WIN"],counts["LOSS"])!=(37,26):raise RuntimeError("Not the user selected 37/26 cohort")
    di20=[x for x in subset if x["di_plus"]<20]
    di19=[x for x in subset if x["di_plus"]<=19.82441408711769]
    di21=[x for x in subset if x["di_plus"]<21]
    old3=[x for x in subset if x["btc_prior15_pct"]<=.062055813
          and x["completed_15m_change_pct"]>=-.10322961 and x["inside_bar"]==0]
    if Counter(x["outcome"] for x in old3)!=Counter({"WIN":20}):
        raise RuntimeError("Original 20 wins zero losses not reproduced")
    combined=[x for x in old3 if x["di_plus"]<20]
    di_g=[x["di_plus"] for x in subset if x["outcome"]=="WIN"]
    di_l=[x["di_plus"] for x in subset if x["outcome"]=="LOSS"]
    chosen_g=[x["di_plus"] for x in old3]
    r={"source":"ONLY original fixed 37 wins 26 losses SHORT trades, frozen public BloFin 390-coin OHLCV shards; no extra market data",
       "baseline_37_26":summary(subset),
       "di_plus_lt20":summary(di20),
       "di_plus_lte19p8244":summary(di19),
       "di_plus_lt21":summary(di21),
       "old_three_condition_rule_20_0":summary(old3),
       "old_three_condition_rule_and_di_plus_lt20":summary(combined),
       "median_di_plus_winners":statistics.median(di_g),
       "median_di_plus_losers":statistics.median(di_l),
       "di_plus_range_all_winners":[min(di_g),max(di_g)],
       "di_plus_range_all_losers":[min(di_l),max(di_l)],
       "di_plus_values_previous_20_winners":sorted(chosen_g),
       "note":"Preselected cutoff 20 comes from 10 different TOP10 coin labels (ex-post 1h price direction), not optimized in this 37/26 source. This checks if it generalizes within user's selected 37/26. All 37/26 results previously mined using known outcomes; no proof of live edge, no slippage/spread/funding. Independent trades may overlap, summed percentage points are not portfolio ROI. LIVE untouched."
       }
    Path("short_3w7l_di20_check_on_chosen37_26_readonly_report.json").write_text(json.dumps(r,indent=2,ensure_ascii=False)+"\n")
    print("DI_PLUS_20_ON_37_26="+json.dumps({k:v for k,v in r.items() if k!="di_plus_values_previous_20_winners"},separators=(",",":"),ensure_ascii=False),flush=True)
    print("DI_PLUS_OLD_WINNERS="+json.dumps(r["di_plus_values_previous_20_winners"],separators=(",",":")),flush=True)
if __name__=="__main__":main()
