"""ONLY selected 37 winning and 26 losing SHORT positions. READ ONLY.
Fast bounded, audited pattern search plus decision tree;
no external trades assessed, no LIVE code/settings changed.
"""
from __future__ import annotations
import json,time
from pathlib import Path
import short37_26_exclusive_only_winner_rules as source

def main():
    rows,keys,missing=source.feature_universe()
    masks,W,L=source.build_thresholds(rows,keys)
    print("INPUT="+json.dumps({"wins":37,"losses":26,"features":len(keys),"unique_threshold_masks":len(masks)}),flush=True)
    # All 1- and 2-condition conjunctions over actual threshold masks.
    positive={}
    shortlist={}
    total=0
    for i,(ma,ca) in enumerate(masks):
        if not (ma&L) and (ma&W).bit_count()>=5:
            positive[ma]=(ca,)
        for mb,cb in masks[i+1:]:
            if ca[0]==cb[0]:continue
            total+=1
            both=ma&mb
            wins=(both&W).bit_count()
            if wins<5:continue
            losers=(both&L).bit_count()
            if not losers:
                positive.setdefault(both,(ca,cb))
            elif losers<=5 and wins>=10:
                shortlist.setdefault(both,(ca,cb,wins,losers))
    print("PAIRS="+json.dumps({"evaluated":total,"distinct_pure_positive_subsets":len(positive),
          "near_pure_pairs":len(shortlist)}),flush=True)
    # Strictly bounded 3rd condition search; avoid retaining millions of equivalent rules.
    shortlist=sorted(shortlist.items(),key=lambda z:(-z[1][2],z[1][3]))[:350]
    triples=0
    for mask,(ca,cb,w,l) in shortlist:
        for mc,cc in masks:
            if cc[0] in (ca[0],cb[0]):continue
            together=mask&mc
            winner=(together&W).bit_count()
            if winner<5:continue
            triples+=1
            if not together&L and together not in positive:
                positive[together]=(ca,cb,cc)
    best=sorted(positive.items(),key=lambda p:(-(p[0]&W).bit_count(),len(p[1])))
    summaries=[]
    for mask,clause in best[:25]:
        summaries.append({"wins":(mask&W).bit_count(),"losses":(mask&L).bit_count(),
             "clauses":[source.desc(c) for c in clause]})
    # All pure subsets combined: Can OR rules cover all 37 wins in-sample?
    combined=0
    for mask in positive: combined|=mask&W
    decision=source.trees(rows,keys)
    simple=decision["simplest_full_separation"]
    report={
     "cohort":"ONLY 37 winning, 26 losing, from chosen SHORT 3-4x volume and rising 2.5h pretrend",
     "features_tested":len(keys),"distinct_single_threshold_masks":len(masks),
     "pair_combinations_evaluated":total,
     "near_pure_pairs_used_in_triple_search":len(shortlist),
     "third_conditions_evaluated":triples,
     "distinct_zero_loss_rules":len(positive),
     "pure_patterns_cover_distinct_wins":combined.bit_count(),
     "best_25_zero_loss_rules":summaries,
     "simplest_tree_that_separates_all_37_from_all_26_in_sample":simple,
     "tree_experiments":decision["searched_models"],
     "warning":"Every rule fitted specifically to outcomes of these 63 known trades; 0/26 historical losses is not a guarantee for future trades. Three-clause search is bounded, one-/two-clause enumerations are exhaustive on available OHLCV features/thresholds. No unobserved order-book data available, no LIVE changes, no comparison with other trades/days."
    }
    print("STRICT37_26_FINAL="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
    Path("strict37_26_only_final_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
if __name__=="__main__":main()
