"""Read-only discovery of profitable volume-dominance strategies on 5m candles.

Train on the first 48h, evaluate fixed ranked candidates on the following 24h.
No broker credentials, no order placement, no LIVE bot files modified.
Strict volume trend: opposite-colored candle must exceed largest anchor candle.
"""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
import json
import time

import blofin_volume_peak_trend_backtest_readonly as old

FIVE=old.FIVE
WINDOW_HOURS=72
TRAIN_HOURS=48
NOTIONAL=20.0
FEE_PCT=0.12
# Time-based and price-based scenarios are evaluated on identical 5m OHLCV.
TP_VALUES=(0.005,0.007,0.01)
SL_VALUES=(0.0025,0.005,0.01)
HOLDS=(1,2,3) # 5,10,15m
VOL_RATIOS=(0.75,1.0,1.5,2.0)
BODY_PCTS=(0,0.10,0.25)
FAMILIES=("FLIP","NEW_PEAK","FOLLOW","SMA","SMA_ALIGNED","EARLY")
MA_AGREES=(False,True)
EARLY_AGES=(3,6,12)  # 15,30,60 min
MIN_TRAIN=10
MIN_TEST=5


def outcome(bars,i,side,tp,sl,hold):
    if i+hold>=len(bars):
        return None
    entry=bars[i+1]["o"]
    if entry<=0:
        return None
    for j in range(i+1,i+hold+1):
        bar=bars[j]
        if side=="LONG":
            hit_tp=bar["h"]>=entry*(1+tp)
            hit_sl=bar["l"]<=entry*(1-sl)
        else:
            hit_tp=bar["l"]<=entry*(1-tp)
            hit_sl=bar["h"]>=entry*(1+sl)
        if hit_sl:  # conservative first-touch ambiguity
            return (-sl*100,bar["ts"]+FIVE)
        if hit_tp:
            return (tp*100,bar["ts"]+FIVE)
    exit_close=bars[i+hold]["c"]
    pnl=(exit_close/entry-1)*100*(1 if side=="LONG" else -1)
    return (pnl,bars[i+hold]["ts"]+FIVE)


def ma_info(bars,i):
    cs=[b["c"] for b in bars[i-20:i+1]]
    prev10=sum(cs[-11:-1])/10
    prev20=sum(cs[-21:-1])/20
    ma10=sum(cs[-10:])/10
    ma20=sum(cs[-20:])/20
    curr="LONG" if ma10>ma20 else "SHORT" if ma10<ma20 else None
    crossed=("LONG" if prev10<=prev20 and ma10>ma20
             else "SHORT" if prev10>=prev20 and ma10<ma20 else None)
    return curr,crossed


def build_candidates(bars,start,end):
    states=old.regime_series(bars)
    age=0
    prev_dir=None
    previous_peak=0.0
    records=[]
    for i in range(35,len(bars)-4):
        b=bars[i]
        ms=b["ts"]+FIVE
        regime,flip,peak=states[i]
        if regime!=prev_dir:
            age=0
        else:
            age+=1
        previous_peak=states[i-1][2]
        prev_dir=regime
        if regime is None or not(start<=ms<end):
            continue
        candle_side=old.classify_candle(b)
        ma,cross=ma_info(bars,i)
        v=b["v"]
        vol20=sum(x["v"] for x in bars[i-20:i])/20
        if vol20<=0:
            continue
        body=100*abs(b["c"]/b["o"]-1) if b["o"]>0 else 0
        prev_volume=bars[i-1]["v"]
        # Each family is derived causally from information known at bar close.
        family_sides={}
        if flip:
            family_sides["FLIP"]=regime
        if candle_side==regime and v>previous_peak and not flip:
            family_sides["NEW_PEAK"]=regime
        if candle_side==regime and v>prev_volume:
            family_sides["FOLLOW"]=regime
        if cross:
            family_sides["SMA"]=cross
            if cross==regime:
                family_sides["SMA_ALIGNED"]=cross
        if candle_side==regime and age<=12 and v>prev_volume:
            family_sides["EARLY"]=regime
        for family,side in family_sides.items():
            if side is None:
                continue
            records.append({"i":i,"ms":ms,"family":family,"side":side,
                            "volume_ratio":v/vol20,"body_pct":body,
                            "ma_agrees":ma==side,
                            "age":age,"bars":bars})
    return records


def one_position(events,tp,sl,hold):
    # Simulation serializes trades across different symbols globally.
    # Sorting by timestamp then symbol is deterministic, NOT a reconstructed ranking.
    endtime=-1
    pnls=[]
    for event in events:
        if event["ms"]<endtime:
            continue
        x=event.get("outcomes",{}).get((tp,sl,hold))
        if x is None:
            continue
        pct,endtime=x
        pnls.append(pct)
    return pnls


def score(pnls):
    n=len(pnls)
    if not n:
        return {"trades":0,"gross_usdt":0,"net_usdt":0,"win_pct":None,
                "after_fee_win_pct":None,"avg_net_pct":None}
    gross=sum(pnls)*NOTIONAL/100
    fee=n*NOTIONAL*FEE_PCT/100
    return {"trades":n,"gross_usdt":round(gross,4),
            "fees_usdt":round(fee,4),
            "net_usdt":round(gross-fee,4),
            "win_pct":round(100*sum(x>0 for x in pnls)/n,2),
            "after_fee_win_pct":round(100*sum(x>FEE_PCT for x in pnls)/n,2),
            "avg_net_pct":round(sum(pnls)/n-FEE_PCT,4)}


def metrics_for_rule(rows,rule,cfg,train_end):
    family,volmin,bodymin,ma_mode,age_limit=rule
    tp,sl,hold=cfg
    eligible=[x for x in rows if x["family"]==family
              and x["volume_ratio"]>=volmin
              and x["body_pct"]>=bodymin
              and (not ma_mode or x["ma_agrees"])
              and (not age_limit or x["age"]<=age_limit)]
    train=[x for x in eligible if x["ms"]<train_end]
    test=[x for x in eligible if x["ms"]>=train_end]
    return score(one_position(train,tp,sl,hold)),score(one_position(test,tp,sl,hold))


def main():
    state=json.loads(Path("blofin_live_state.json").read_text())
    end=(int(time.time()*1000)//FIVE)*FIVE
    start=end-WINDOW_HOURS*60*60_000
    train_end=start+TRAIN_HOURS*60*60_000
    symbols=[]
    actual=[]
    for t in state.get("trade_history",[]):
        try:
            ms=int(t.get("opened_ms",0))
            rank=int(t.get("signal_rank") or 0)
            inst=t.get("inst")
        except (ValueError,TypeError,AttributeError):
            continue
        if start<=ms<end and 51<=rank<=100 and inst:
            actual.append(t)
            if inst not in symbols:
                symbols.append(inst)
    if not symbols:
        raise RuntimeError("No traded symbols in window")
    candles={}
    errors={}
    with ThreadPoolExecutor(max_workers=5) as pool:
        fs={pool.submit(old.fetch_candles,inst,end,start-120*FIVE):inst for inst in symbols}
        for future in as_completed(fs):
            inst=fs[future]
            try:
                chunks,_=future.result()
                if chunks:
                    candles[inst]=chunks
                else:
                    errors[inst]="Empty contiguous history"
            except Exception as exc:
                errors[inst]=str(exc)
    print(f"COINS_FETCHED={len(candles)} COINS_FAILED={len(errors)} ACTUAL_TRADES={len(actual)}",flush=True)

    events=[]
    for inst, chunks in candles.items():
        for bars in chunks:
            for x in build_candidates(bars,start,end):
                event={k:v for k,v in x.items() if k!="bars"}
                event["inst"]=inst
                i=x["i"]
                side=x["side"]
                event["outcomes"]={}
                for tp,sl,hold in product(TP_VALUES,SL_VALUES,HOLDS):
                    ret=outcome(bars,i,side,tp,sl,hold)
                    if ret is not None:
                        event["outcomes"][(tp,sl,hold)]=ret
                events.append(event)
    events.sort(key=lambda x:(x["ms"],x["inst"],x["family"]))
    print(f"SIGNALS_READY={len(events)}",flush=True)

    # Train selection uses ONLY first 48h; holdout remains unseen for ranking.
    ranked=[]
    rules=[]
    for fam in FAMILIES:
        age_choices=(3,6,12) if fam in ("EARLY","FOLLOW","NEW_PEAK") else (0,)
        for vr,body,ma_mode,age in product(VOL_RATIOS,BODY_PCTS,MA_AGREES,age_choices):
            rules.append((fam,vr,body,ma_mode,age))
    runs=len(rules)*len(TP_VALUES)*len(SL_VALUES)*len(HOLDS)
    print(f"RULES={len(rules)} PARAMETER_COMBINATIONS={runs}",flush=True)
    # Group by family to reduce repeated filtering.
    family_data={f:[x for x in events if x["family"]==f] for f in FAMILIES}
    for rule in rules:
        fam,vr,body,ma_mode,age_limit=rule
        eligible=[x for x in family_data[fam]
                  if x["volume_ratio"]>=vr and x["body_pct"]>=body
                  and (not ma_mode or x["ma_agrees"])
                  and (not age_limit or x["age"]<=age_limit)]
        train=[x for x in eligible if x["ms"]<train_end]
        test=[x for x in eligible if x["ms"]>=train_end]
        # Do not read/test on holdout during model selection; only calculate
        # later for top training configurations.
        for cfg in product(TP_VALUES,SL_VALUES,HOLDS):
            tr=score(one_position(train,*cfg))
            if tr["trades"]>=MIN_TRAIN:
                ranked.append({"rule":rule,"exit":cfg,"train":tr})
    ranked.sort(key=lambda x:(x["train"]["avg_net_pct"],
                             x["train"]["net_usdt"]),reverse=True)
    checked=[]
    # Evaluate top N ranked by training, and add unfiltered baselines for reference.
    top=ranked[:30]
    # Include highest train absolute net and every family winner for validation.
    top += sorted(ranked,key=lambda x:x["train"]["net_usdt"],reverse=True)[:10]
    for fam in FAMILIES:
        candidates=[x for x in ranked if x["rule"][0]==fam]
        if candidates:
            top.append(max(candidates,key=lambda x:(x["train"]["net_usdt"],x["train"]["avg_net_pct"])))
    seen=set()
    for found in top:
        key=(tuple(found["rule"]),tuple(found["exit"]))
        if key in seen: continue
        seen.add(key)
        tr,te=metrics_for_rule(family_data[found["rule"][0]],found["rule"],found["exit"],train_end)
        record={"family":found["rule"][0],
                "min_volume_vs_sma20":found["rule"][1],
                "min_candle_body_pct":found["rule"][2],
                "require_ma_trend_agrees":found["rule"][3],
                "regime_age_limit_candles":found["rule"][4],
                "tp_pct":100*found["exit"][0],
                "sl_pct":100*found["exit"][1],
                "max_hold_minutes":5*found["exit"][2],
                "training_48h":tr,"holdout_24h":te}
        checked.append(record)
    # Honest OOS report; positive OOS not enough without adequate samples.
    viable=[r for r in checked if r["training_48h"]["net_usdt"]>0
            and r["holdout_24h"]["net_usdt"]>0
            and r["holdout_24h"]["trades"]>=MIN_TEST]
    viable.sort(key=lambda r:(r["holdout_24h"]["net_usdt"],
                             r["training_48h"]["net_usdt"]),reverse=True)
    train_positive=sum(x["train"]["net_usdt"]>0 for x in ranked)
    baselines={}
    for family in ("FLIP","SMA","SMA_ALIGNED","FOLLOW","NEW_PEAK","EARLY"):
        for cfg in ((0.01,0.0025,2),(0.007,0.01,2),(0.005,0.005,2)):
            rulespec=(family,0.75,0.0,False,0)
            tr,te=metrics_for_rule(family_data[family],rulespec,cfg,train_end)
            baselines[f"{family}_TP{100*cfg[0]:g}_SL{100*cfg[1]:g}_H{5*cfg[2]}"]={"training":tr,"holdout":te}

    report={"generated_utc":datetime.now(timezone.utc).isoformat(),
            "window_utc":{"start":datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),
                          "train_end":datetime.fromtimestamp(train_end/1000,timezone.utc).isoformat(),
                          "end":datetime.fromtimestamp(end/1000,timezone.utc).isoformat()},
            "coins_requested":len(symbols),"coins_analyzed":len(candles),
            "failed_coins":errors,"actual_trades":len(actual),
            "signals_by_family":dict((f,len(family_data[f])) for f in FAMILIES),
            "parameter_combinations_checked":runs,
            "train_models_with_10_or_more_trades":len(ranked),
            "train_profitable_models":train_positive,
            "top_training_then_holdout":checked[:25],
            "positive_train_and_holdout_five_or_more_trades":viable[:12],
            "baseline":baselines,
            "fee_pct_assumed_roundtrip":FEE_PCT,
            "limitations":"72h small sample, many candidate parameter tests risk overfitting; 24h holdout initially not used for fitting but showing favorable holdout involves multiple comparisons; 45+ chosen coins based on their trading in future window (selection bias); ranked 51-100 not reconstructed by date; simulator not actual LIVE rules, orderbook, variable trailing SL, fees modeled at flat 0.12%, no spread/slippage; no entry until next 5m open; simultaneous signals resolved lexicographic coin order."}
    Path("volume_profit_search_readonly_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print("PROFIT_SEARCH_RESULT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    main()
