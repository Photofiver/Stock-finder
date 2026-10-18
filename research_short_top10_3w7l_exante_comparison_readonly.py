"""READ ONLY: historical ex-ante features of three profitable SHORTs versus seven
unprofitable SHORTs from fixed BloFin TOP10 on 2026-10-18 18:35 to 19:35 UK.
No orders, no secrets. Every indicator calculated from 5m CLOSED CANDLES at
or before 18:30 UK. Entire subsequent 60min price move is outcome ONLY.

WARNING: TOP10 itself is from post-window 19:39 ranking; hindsight selection.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import statistics,json,math
import blofin_top200_repeat_setup_research as api
from short37_extra_indicators_readonly import features

F=300000
UK=ZoneInfo("Europe/London")
START=int(datetime(2026,10,18,18,35,tzinfo=UK).timestamp()*1000)
END=int(datetime(2026,10,18,19,35,tzinfo=UK).timestamp()*1000)
COINS=["DGAI-USDT","XMR-USDT","ARC-USDT","IOTX-USDT","SENT-USDT",
       "OPENAI-USDT","BTCDOM-USDT","XDP-USDT","ANTHROPIC-USDT","GWEI-USDT"]
GOOD={"XMR-USDT","OPENAI-USDT","GWEI-USDT"}
FEE=0.12
def get(inst):
    # Fetch up to 200 closed bars BEFORE entry plus 12 during the hour.
    data,pages=api.candles(inst,END+F,START-185*F)
    if len(data)<190 or data.get(START-F) is None or data.get(START-179*F) is None or data.get(END-F) is None:
        raise RuntimeError(f"Incomplete historical 5m candles: {inst}, {len(data)}")
    return inst,data,pages
def median(r,key):
    vals=[x[key] for x in r if x[key] is not None]
    return round(statistics.median(vals),5) if vals else None
def main():
    api.MIN_CALL_GAP=.16
    maps={}
    with ThreadPoolExecutor(max_workers=5) as pool:
        fs={pool.submit(get,c):c for c in COINS+["BTC-USDT"]}
        for fu in as_completed(fs):
            coin=fs[fu]
            _,market,pages=fu.result()
            maps[coin]=market
    rows=[]
    for rank,coin in enumerate(COINS,1):
        market=maps[coin]
        ent=market[START][0]
        out=market[END-F][3]
        gross=100*(1-out/ent)
        net=gross-FEE
        label="WIN" if net>0 else "LOSS"
        if (coin in GOOD)!=(label=="WIN"):
            raise RuntimeError(f"Nonreproducible prior 3/7: {coin}, {net}")
        feat=features({"entry_ms":START,"rank":rank,
                       "inst":coin,"outcome":label},
                       market,maps["BTC-USDT"])
        if feat is None:raise RuntimeError("Insufficient technical features "+coin)
        rows.append({**feat,"entry_open":ent,"exit_close":out,
                   "future_hourly_short_gross_pct":gross,
                   "future_hourly_short_net_pct":net})
    wins=[x for x in rows if x["outcome"]=="WIN"]
    losses=[x for x in rows if x["outcome"]=="LOSS"]
    if len(wins)!=3 or len(losses)!=7:raise RuntimeError("Bad groups")
    feature_keys=sorted(k for k in rows[0] if k not in (
      "entry_ms","coin","outcome","rank","entry_open","exit_close",
      "future_hourly_short_gross_pct","future_hourly_short_net_pct","btc_prior15_pct",
      "btc_prior60_pct"))
    prioritized=[
      "price_prior15_pct","price_prior60_pct","price_prior150_pct",
      "completed_15m_change_pct","completed_1h_change_pct","previous_15m_change_pct",
      "coin_minus_btc_15m","coin_minus_btc_1h",
      "rsi14","rsi14_delta","macd_hist_pct","macd_hist_delta_pct",
      "stoch_k","stoch_k_minus_d","adx14","di_plus","di_minus",
      "di_minus_above_plus","vol_to_mean10","vol_to_prev","vol_to_mean20",
      "red_body_pct","red_engulfing","inside_bar",
      "upper_wick_ratio","lower_wick_ratio","distance_to_resistance20_pct",
      "distance_to_support20_pct","vwap20_diff_pct","obv10_signed_to_mean20"]
    comparisons=[]
    purecuts=[]
    for key in feature_keys:
        w=[r[key] for r in wins if r[key] is not None]
        l=[r[key] for r in losses if r[key] is not None]
        if len(w)!=3 or len(l)!=7:continue
        allv=sorted(set(float(r[key]) for r in rows))
        if len(allv)<2:continue
        candidates=[]
        for threshold in allv:
            for op in ("<=",">="):
                wpass=sum(float(v)<=threshold if op=="<=" else float(v)>=threshold for v in w)
                lpass=sum(float(v)<=threshold if op=="<=" else float(v)>=threshold for v in l)
                if wpass<2:continue
                if wpass==3 and lpass==7:continue
                quality=(wpass/3)-(lpass/7)
                candidates.append((quality,wpass,-lpass,op,threshold))
                if lpass==0:purecuts.append({"indicator":key,"condition":op,
                      "threshold":threshold,"wins":wpass,"losses":lpass})
        best=max(candidates) if candidates else None
        comparisons.append({"indicator":key,
          "win_values":[round(v,5) for v in w],
          "loss_values":[round(v,5) for v in l],
          "winner_median":median(wins,key),"loser_median":median(losses,key),
          "best_cut":{"condition":best[3],"threshold":round(best[4],7),
                      "wins_captured":best[1],"losses_captured":-best[2],
                      "separation_score":round(best[0],5)} if best else None})
    comparisons.sort(key=lambda z:-(z["best_cut"]["separation_score"] if z["best_cut"] else -1))
    purecuts.sort(key=lambda z:(-z["wins"],z["indicator"],z["threshold"]))
    priority_comp=[next((a for a in comparisons if a["indicator"]==key),None)
                   for key in prioritized]
    priority_comp=[a for a in priority_comp if a]
    # Evaluate OLD 3 filters as of 18:30, without original entry-volume trigger,
    # to see if they provide any signal on the fixed 10 coins.
    threshold_s=[
      ("BTC15 <=",lambda r:r["btc_prior15_pct"]<=.062055813),
      ("coin completed15 >=",lambda r:r["completed_15m_change_pct"]>=-.10322961),
      ("not inside",lambda r:r["inside_bar"]==0)]
    simple=[]
    for x in rows:
        booleans={name:bool(fn(x)) for name,fn in threshold_s}
        simple.append({"coin":x["coin"],"outcome":x["outcome"],"passes":booleans,
                       "all_three":all(booleans.values())})
    g=Counter(x["outcome"] for x in rows)
    cg=Counter(x["outcome"] for x in rows if next(s["all_three"] for s in simple if s["coin"]==x["coin"]))
    report={"window_uk":"2026-10-18 18:35–19:35",
      "asof_features_uk":"2026-10-18 18:30 closed 5m",
      "group_counts":{"winning_shorts":g["WIN"],"losing_shorts":g["LOSS"]},
      "fee_roundtrip_pct":FEE,
      "btc15_pct_same_all":rows[0]["btc_prior15_pct"],
      "btc60_pct_same_all":rows[0]["btc_prior60_pct"],
      "features_compared":len(comparisons),
      "selected_3_condition_filter":simple,
      "selected_3_condition_counts_if_ignore_3x4x_volume":{
        "win":cg["WIN"],"loss":cg["LOSS"]},
      "top_single_feature_splits_exploratory":comparisons[:24],
      "best_simple_winner_only_thresholds":purecuts[:35],
      "selected_indicators":priority_comp,
      "per_coin":rows,
      "limitations":"ONLY 10 coins: thresholds exhaustively chosen AFTER known future outcomes; any clean 3/0 boundary is extreme overfit and has no demonstrated predictive power. Top10 chosen from END ranking 19:39 (selection hindsight). Hypothetical SHORT held for a full hour, not actual volume-reversal strategy entry/exit. BTC same for all ten at same time, not cross-sectional discriminator. 0.12% fee only; no slippage, spread, funding. LIVE untouched."}
    Path("research_short_top10_3wins_7losses_exante_comparison.json").write_text(
      json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print("GROUPS="+json.dumps({"winning":g["WIN"],"losing":g["LOSS"],"features":len(comparisons),
      "btc15_pct":rows[0]["btc_prior15_pct"],
      "old3filters_no_vol":{"win":cg["WIN"],"loss":cg["LOSS"]}},separators=(",",":")),flush=True)
    print("BEST_PREENTRY_SPLITS="+json.dumps(comparisons[:14],separators=(",",":")),flush=True)
    print("PURE_CUTS="+json.dumps(purecuts[:25],separators=(",",":")),flush=True)
    print("SELECTED_FEATURES="+json.dumps(priority_comp,separators=(",",":")),flush=True)
    print("EACH_COIN="+json.dumps([
        {"coin":x["coin"],"outcome":x["outcome"],
         "hour_short_net_pct":round(x["future_hourly_short_net_pct"],3),
         **{k:round(x[k],4) if x.get(k) is not None else None for k in prioritized}}
        for x in rows],separators=(",",":")),flush=True)
if __name__=="__main__":main()
