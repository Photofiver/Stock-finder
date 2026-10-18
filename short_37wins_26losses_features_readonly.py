"""READ ONLY: 37 winning vs 26 losing SHORT signals with pre-entry 2.5h price rise.
Reuses completed public BloFin OHLCV shards. Entry-only features, avoid leakage.
"""
from collections import Counter
import json
import statistics
from pathlib import Path
import short60_volume_mean10_24hour_readonly as m
from short60_subset_3to4x_winloss_explanation import features

def med(a):
    return round(statistics.median(a),4) if a else None

def stats(x,keys):
    return {k:med([z[k] for z in x if z[k] is not None]) for k in keys}

def split(rows, name, test):
    true=[z for z in rows if test(z)]
    false=[z for z in rows if not test(z)]
    def count(g):
        w=sum(z["outcome"]=="WIN" for z in g)
        l=sum(z["outcome"]=="LOSS" for z in g)
        return {"wins":w,"losses":l,"winrate_pct":round(100*w/(w+l),2) if w+l else None}
    return {name:{"yes":count(true),"no":count(false)}}

def main():
    data,err,total=m.load()
    assert len(data)==390 and not err
    ranks,_=m.rankings(data)
    tested=m.test_period(data,ranks,m.START,m.END,1.6)
    assert (tested["wins"],tested["losses"])==(528,649)
    b=[t for t in tested["trades"] if
        t["outcome"] in ("WIN","LOSS") and t["volume_ratio"] is not None
        and 3<=t["volume_ratio"]<4]
    assert (sum(t["outcome"]=="WIN" for t in b),sum(t["outcome"]=="LOSS" for t in b))==(64,78)
    allfeatures=[features(t,data[t["inst"]]) for t in b]
    assert all(z is not None for z in allfeatures)
    cohort=[z for z in allfeatures if z["price_change_prior30_pct"]>=0]
    winning=[z for z in cohort if z["outcome"]=="WIN"]
    losing=[z for z in cohort if z["outcome"]=="LOSS"]
    assert (len(winning),len(losing))==(37,26), (len(winning),len(losing))
    keys=[
     "price_change_prior30_pct","price_change_prior12_pct",
     "price_change_prior6_pct","price_change_prior3_pct",
     "entry_volume_to_previous","entry_volume_to_mean10",
     "signal_red_body_pct","signal_body_fraction_of_range",
     "previous_green_body_pct","prior6_red_count","rsi14_simple",
     "sma10_minus_sma20_pct","sma10_slope_pct","rank",
     "change_price_next5_pct","holding_min"
    ]
    compared={"WIN":stats(winning,keys),"LOSS":stats(losing,keys)}
    checks=[
      ("price_2_5h_gain_ge_0p25pct",lambda z:z["price_change_prior30_pct"]>=0.25),
      ("price_2_5h_gain_ge_0p5pct",lambda z:z["price_change_prior30_pct"]>=0.5),
      ("price_2_5h_gain_ge_1pct",lambda z:z["price_change_prior30_pct"]>=1),
      ("last_30min_price_falling",lambda z:z["price_change_prior6_pct"]<0),
      ("last_60min_price_falling",lambda z:z["price_change_prior12_pct"]<0),
      ("last_15min_price_falling",lambda z:z["price_change_prior3_pct"]<0),
      ("rsi14_below50",lambda z:z["rsi14_simple"]<50),
      ("rsi14_below45",lambda z:z["rsi14_simple"]<45),
      ("rsi14_above55",lambda z:z["rsi14_simple"]>=55),
      ("sma10_below_sma20",lambda z:z["sma10_minus_sma20_pct"]<0),
      ("volume_entry_ge_0p5_mean10",lambda z:z["entry_volume_to_mean10"]>=.5),
      ("volume_entry_below_0p3_mean10",lambda z:z["entry_volume_to_mean10"]<.3),
      ("volume_ratio_prev_ge3p5",lambda z:z["entry_volume_to_previous"]>=3.5),
      ("signal_red_body_ge0p15pct",lambda z:z["signal_red_body_pct"]>=0.15),
      ("signal_red_body_ge0p3pct",lambda z:z["signal_red_body_pct"]>=0.30),
      ("previous_green_body_le0p1pct",lambda z:z["previous_green_body_pct"]<=0.1),
      ("signal_red_body_gt_previous_green_body",lambda z:z["signal_red_body_pct"]>z["previous_green_body_pct"]),
      ("red_candle_body_ge_70pct_range",lambda z:z["signal_body_fraction_of_range"]>=.7),
      ("prior6_majority_green",lambda z:z["prior6_red_count"]<=2),
      ("prior6_majority_red",lambda z:z["prior6_red_count"]>=4),
      ("rank_100_to_200",lambda z:z["rank"]>=100),
      ("rank_150_to_200",lambda z:z["rank"]>=150),
    ]
    tests={name:split(cohort,name,fn)[name] for name,fn in checks}
    combos=[
      ("price_prior30_up_and_last30_down_and_red_gt_green",lambda z:z["price_change_prior6_pct"]<0 and z["signal_red_body_pct"]>z["previous_green_body_pct"]),
      ("price_prior30_up_and_sma10_below20_and_last30_down",lambda z:z["sma10_minus_sma20_pct"]<0 and z["price_change_prior6_pct"]<0),
      ("price_prior30_up_and_rsi_lt50_and_red_gt_green",lambda z:z["rsi14_simple"]<50 and z["signal_red_body_pct"]>z["previous_green_body_pct"]),
      ("price_prior30_up_and_rank100plus_and_last30_down",lambda z:z["rank"]>=100 and z["price_change_prior6_pct"]<0)
    ]
    combo={name:split(cohort,name,fn)[name] for name,fn in combos}
    result={
       "cohort_rule":"SHORT, signal volume 3x-4x previous green candle and <=mean previous 10, price rose or stayed flat during last 30 closed 5m candles (2.5h). Entries at next bar open.",
       "wins":37,"losses":26,"median_compare":compared,
       "individual_entry_checks":tests,"simple_combinations_exploratory":combo,
       "sample_trades":[{"coin":z["coin"],"time_uk":m.clock(z["entry_ms"]),"profit_pct":z["profit_pct"],"vol_prev":z["entry_volume_to_previous"],"vol_mean":z["entry_volume_to_mean10"],"prior2_5h_pct":round(z["price_change_prior30_pct"],3),"rsi":round(z["rsi14_simple"],2)} for z in winning[:12]],
       "warning":"In-sample 37 vs 26; multiple post-hoc comparisons; these features do NOT guarantee success nor prove why wins happened. No fees or spread, concurrent coin trades, no live edits."
    }
    print("GROUP_37_VS_26="+json.dumps({k:v for k,v in result.items() if k!="sample_trades"},separators=(",",":"),ensure_ascii=False),flush=True)
    Path("short_37wins_26losses_features.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
if __name__=="__main__":main()
