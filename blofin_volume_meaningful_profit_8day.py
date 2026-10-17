"""READ-ONLY multi-day search for meaningful NET PROFIT from peak volume regimes.

Purpose: explore higher-quality entries *within user's volume-defined trend*,
not merely block losing LIVE positions. First 4d train, next 2d validation,
last 2d untouched holdout. Only choose candidate from train+validation; holdout
is inspected once, never searched/optimized. Simulated spread/slippage excluded,
fixed 0.12% round-trip fees, 20 USDT flat per entry, one position globally.
No LIVE exchange keys or trade endpoint used.
"""
from __future__ import annotations
import collections
import concurrent.futures
import datetime as dt
import itertools
import json
import math
import statistics
import time
from pathlib import Path

import blofin_volume_peak_trend_backtest_readonly as vol

FIVE=300000
WINDOW_DAYS=8
TRAIN_DAYS=4
VALIDATION_DAYS=2
NOTIONAL=20.
ROUNDTRIP_FEE_PCT=0.12
TPS=(0.005,0.007,0.01,0.015)
SLS=(0.0025,0.005,0.0075,0.01)
HOLDS=(1,2,3,4)
EXITS=list(itertools.product(TPS,SLS,HOLDS))
VOL_THRESHOLD=(0.75,1.25,2.0)
BODY_THRESHOLD=(0.,0.15,0.3)
DIRECTIONS=("BOTH","LONG","SHORT")
MA_MODES=("ANY","ALIGNED")
RSI_MODES=("ANY","DIRECTION")
MIN_TRAIN=20
MIN_VALID=10
MIN_HOLDOUT=10
FAMILIES=("FLIP","PEAK","FOLLOW","CONTINUATION","PULLBACK_RESUME",
          "BREAKOUT6","BREAKOUT12","SMA_ALIGNED","VOLUME_SURGE")


def fetch_8days(inst,end,earliest):
    rows={}
    after=end
    for _ in range(24):
        page=vol.get_page(inst,after)
        if not page:
            break
        for b in page:
            if b["ts"]+FIVE <= end:
                rows[b["ts"]]=b
        oldest=min(x["ts"] for x in page)
        if oldest < earliest or oldest>=after:
            break
        after=oldest
    ordered=sorted(rows.values(),key=lambda x:x["ts"])
    groups=[]
    run=[]
    for b in ordered:
        if run and b["ts"]-run[-1]["ts"]!=FIVE:
            if len(run)>=65:
                groups.append(run)
            run=[]
        run.append(b)
    if len(run)>=65:
        groups.append(run)
    return groups


def rsi_series(bars,n=14):
    out=[None]*len(bars)
    if len(bars)<n+1: return out
    g=0.
    l=0.
    for i in range(1,n+1):
        d=bars[i]["c"]-bars[i-1]["c"]
        g+=max(d,0.)
        l+=max(-d,0.)
    g/=n
    l/=n
    out[n]=100-100/(1+g/l) if l else 100.
    for i in range(n+1,len(bars)):
        d=bars[i]["c"]-bars[i-1]["c"]
        g=(g*(n-1)+max(0.,d))/n
        l=(l*(n-1)+max(0.,-d))/n
        out[i]=100-100/(1+g/l) if l else 100.
    return out


def simulate_exit(bars,i,side,tp,sl,hold):
    if i+hold>=len(bars):
        return None
    entry=bars[i+1]["o"]
    if entry<=0:
        return None
    for j in range(i+1,i+hold+1):
        b=bars[j]
        if side=="LONG":
            hit_tp=b["h"]>=entry*(1+tp)
            hit_sl=b["l"]<=entry*(1-sl)
        else:
            hit_tp=b["l"]<=entry*(1-tp)
            hit_sl=b["h"]>=entry*(1+sl)
        if hit_sl:  # conservative if both TP/SL appear inside same 5m bar
            return (-100*sl,b["ts"]+FIVE,"SL" if not hit_tp else "BOTH_UNKNOWN_AS_SL")
        if hit_tp:
            return (100*tp,b["ts"]+FIVE,"TP")
    final=bars[i+hold]["c"]
    pct=100*(final/entry-1)*(1 if side=="LONG" else -1)
    return (pct,bars[i+hold]["ts"]+FIVE,"TIME")


def build_events(inst,groups,start,end):
    events=[]
    for bars in groups:
        regimes=vol.regime_series(bars)
        rsi=rsi_series(bars)
        prev_dir=None
        age=0
        for i in range(35,len(bars)-5):
            b=bars[i]
            t=b["ts"]+FIVE
            direction,flip,peak=regimes[i]
            if direction!=prev_dir: age=0
            else: age+=1
            prev_dir=direction
            if direction is None or not(start<=t<end):
                continue
            candle_side=vol.classify_candle(b)
            if b["o"]<=0:continue
            body_pct=100*abs(b["c"]/b["o"]-1)
            vol_med=statistics.median(x["v"] for x in bars[i-20:i])
            if vol_med<=0:continue
            vol_ratio=b["v"]/vol_med
            last=bars[i-1]
            closes=[c["c"] for c in bars[i-20:i+1]]
            ma10=sum(closes[-10:])/10
            ma20=sum(closes[-20:])/20
            old_ma10=sum(closes[-11:-1])/10
            old_ma20=sum(closes[-21:-1])/20
            price_dir="LONG" if ma10>ma20 else "SHORT" if ma10<ma20 else None
            cross=("LONG" if old_ma10<=old_ma20 and ma10>ma20
                   else "SHORT" if old_ma10>=old_ma20 and ma10<ma20 else None)
            price_break6=(b["c"]>max(x["h"] for x in bars[i-6:i]) if direction=="LONG"
                          else b["c"]<min(x["l"] for x in bars[i-6:i]))
            price_break12=(b["c"]>max(x["h"] for x in bars[i-12:i]) if direction=="LONG"
                           else b["c"]<min(x["l"] for x in bars[i-12:i]))
            prev_peak=regimes[i-1][2]
            prev_color=vol.classify_candle(last)
            families=[]
            if flip:
                families.append("FLIP")
            if candle_side==direction and b["v"]>prev_peak and not flip:
                families.append("PEAK")
            if candle_side==direction and b["v"]>last["v"]:
                families.append("FOLLOW")
            if candle_side==direction and prev_color==direction and b["c"]>last["c"] if direction=="LONG" else candle_side==direction and prev_color==direction and b["c"]<last["c"]:
                families.append("CONTINUATION")
            if candle_side==direction and prev_color!=direction and b["v"]>last["v"]:
                families.append("PULLBACK_RESUME")
            if candle_side==direction and price_break6:
                families.append("BREAKOUT6")
            if candle_side==direction and price_break12:
                families.append("BREAKOUT12")
            if cross==direction:
                families.append("SMA_ALIGNED")
            if candle_side==direction and vol_ratio>=1.5 and b["v"]>last["v"]:
                families.append("VOLUME_SURGE")
            if not families:continue
            rsi_ok=(rsi[i]>=50 if direction=="LONG" else rsi[i]<=50) if rsi[i] is not None else False
            exit_results={}
            for index,(tp,sl,hold) in enumerate(EXITS):
                simulation=simulate_exit(bars,i,direction,tp,sl,hold)
                if simulation:
                    exit_results[index]=simulation
            if not exit_results:continue
            for family in families:
                events.append({"t":t,"inst":inst,"family":family,"side":direction,
                               "vol_ratio":vol_ratio,"body_pct":body_pct,
                               "ma_aligned":price_dir==direction,
                               "rsi_aligned":rsi_ok,
                               "age":age,"exits":exit_results})
    return events


def score(events,exit_index):
    busy=-1
    pnls=[]
    tp=sl=ambiguous=0
    for e in events:
        if e["t"]<busy:
            continue
        x=e["exits"].get(exit_index)
        if x is None:continue
        p,busy,why=x
        pnls.append(p)
        tp+=why=="TP"
        sl+=why=="SL"
        ambiguous+=why=="BOTH_UNKNOWN_AS_SL"
    n=len(pnls)
    gross=sum(pnls)*NOTIONAL/100
    fees=n*NOTIONAL*ROUNDTRIP_FEE_PCT/100
    return {"trades":n,
            "wins_before_fees":sum(x>0 for x in pnls),
            "wins_after_fees":sum(x>ROUNDTRIP_FEE_PCT for x in pnls),
            "gross_usdt":round(gross,4),
            "fees_usdt":round(fees,4),
            "net_usdt":round(gross-fees,4),
            "net_pct_of_20_usdt":round((gross-fees)/NOTIONAL*100,2),
            "net_per_trade_pct":round(sum(pnls)/n-ROUNDTRIP_FEE_PCT,4) if n else None,
            "tp":tp,"sl":sl,"same_candle_both_counted_sl":ambiguous}


def options_json(key,exit_index,train,valid,holdout=None):
    fam,volume_min,body_min,direction,ma_mode,rsi_mode=key
    tp,sl,hold=EXITS[exit_index]
    return {"family":fam,"min_volume_to_median20":volume_min,
            "min_body_pct":body_min,"direction":direction,
            "ma_filter":ma_mode,"rsi_filter":rsi_mode,
            "tp_pct":100*tp,"sl_pct":100*sl,"hold_minutes":hold*5,
            "training":train,"validation":valid,
            **({"holdout":holdout} if holdout is not None else {})}


def select_by_rule(events,train_start,train_end,valid_end,holdout_end):
    events_by_family={f:[] for f in FAMILIES}
    for e in events:
        events_by_family[e["family"]].append(e)
    for e in events_by_family.values():
        e.sort(key=lambda z:(z["t"],z["inst"]))
    candidates=[]
    checked=0
    for fam,fam_events in events_by_family.items():
        for volmin,bodymin,direction,ma_mode,rsi_mode in itertools.product(VOL_THRESHOLD,BODY_THRESHOLD,DIRECTIONS,MA_MODES,RSI_MODES):
            eligible=[e for e in fam_events
                      if e["vol_ratio"]>=volmin and e["body_pct"]>=bodymin
                      and (direction=="BOTH" or e["side"]==direction)
                      and (ma_mode=="ANY" or e["ma_aligned"])
                      and (rsi_mode=="ANY" or e["rsi_aligned"])]
            train=[e for e in eligible if e["t"]<train_end]
            valid=[e for e in eligible if train_end<=e["t"]<valid_end]
            if len(train)<MIN_TRAIN or len(valid)<MIN_VALID:
                continue
            for exitidx in range(len(EXITS)):
                checked+=1
                tr=score(train,exitidx)
                if tr["trades"]<MIN_TRAIN or tr["net_usdt"]<=0:
                    continue
                va=score(valid,exitidx)
                if va["trades"]<MIN_VALID or va["net_usdt"]<=0:
                    continue
                # Ranking must be computed only on training/validation data.
                metric=min(tr["net_usdt"]*0.5,va["net_usdt"])*(1+min(100,tr["trades"]+va["trades"])/200)
                candidates.append({"key":(fam,volmin,bodymin,direction,ma_mode,rsi_mode),
                                   "exitidx":exitidx,"train":tr,"valid":va,
                                   "metric":metric,
                                   "n":tr["trades"]+va["trades"]})
    candidates.sort(key=lambda x:(x["metric"],x["train"]["net_usdt"]+x["valid"]["net_usdt"]),reverse=True)
    return candidates,checked,events_by_family


def main():
    state=json.loads(Path("blofin_live_state.json").read_text(encoding="utf-8"))
    end=(int(time.time()*1000)//FIVE)*FIVE
    start=end-WINDOW_DAYS*86400000
    train_end=start+TRAIN_DAYS*86400000
    valid_end=train_end+VALIDATION_DAYS*86400000
    # Keep user actual rank 51..100 symbol universe, be explicit about ex-post selection.
    symbols=[]
    for trade in state.get("trade_history",[]):
        if not isinstance(trade,dict):continue
        try:
            rank=int(trade.get("signal_rank") or 0)
            ms=int(trade.get("opened_ms") or 0)
        except (ValueError,TypeError):continue
        inst=trade.get("inst")
        if 51<=rank<=100 and start<=ms<end and inst and inst not in symbols:
            symbols.append(inst)
    if not symbols:
        raise RuntimeError("No actual traded instruments from recent 8 days")
    if len(symbols)>100:raise RuntimeError("Too many symbol names")
    raw={}
    errors={}
    begin=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as workers:
        futures={workers.submit(fetch_8days,inst,end,start-150*FIVE):inst for inst in symbols}
        for future in concurrent.futures.as_completed(futures):
            inst=futures[future]
            try:
                chunks=future.result()
                if not chunks:
                    errors[inst]="NO_CONTIGUOUS_HISTORY"
                else:
                    raw[inst]=chunks
            except Exception as e:
                errors[inst]=str(e)
    print(f"FETCHED={len(raw)} REQUESTED={len(symbols)} FAILED={len(errors)} SECONDS={time.monotonic()-begin:.1f}",flush=True)
    if len(raw)<10:raise RuntimeError("Not enough market histories for reasonable search")

    all_events=[]
    for inst,chunks in raw.items():
        all_events.extend(build_events(inst,chunks,start,end))
    print("CANDIDATE_EVENTS="+str(len(all_events)),flush=True)
    candidates,checked,by_family=select_by_rule(all_events,start,train_end,valid_end,end)
    print(f"RULE_EXITS_CHECKED={checked} POSITIVE_TRAIN_AND_VALIDATION={len(candidates)}",flush=True)
    good=[]
    # Only evaluate holdout for TOP candidate chosen before seeing holdout.
    if candidates:
        for i,candidate in enumerate(candidates[:3]):
            key=candidate["key"]
            fam,volmin,bodymin,direction,ma_mode,rsi_mode=key
            eligible=[e for e in by_family[fam]
                      if valid_end<=e["t"]<end and e["vol_ratio"]>=volmin and e["body_pct"]>=bodymin
                      and (direction=="BOTH" or e["side"]==direction)
                      and (ma_mode=="ANY" or e["ma_aligned"])
                      and (rsi_mode=="ANY" or e["rsi_aligned"])]
            ho=score(eligible,candidate["exitidx"])
            good.append(options_json(key,candidate["exitidx"],candidate["train"],candidate["valid"],ho))
    # Unoptimized reference on same dataset for meaningful scale
    baselines={}
    for fam in ("FLIP","FOLLOW","BREAKOUT6","SMA_ALIGNED"):
        rowset=by_family[fam]
        one=[e for e in rowset if e["t"]<train_end]
        two=[e for e in rowset if train_end<=e["t"]<valid_end]
        three=[e for e in rowset if valid_end<=e["t"]<end]
        cfg=(.007,.01,2)
        idx=EXITS.index(cfg)
        baselines[fam]={"train":score(one,idx),"validation":score(two,idx),"holdout":score(three,idx)}

    summary={
        "utc_generated":dt.datetime.now(dt.timezone.utc).isoformat(),
        "windows":{"start":dt.datetime.fromtimestamp(start/1000,dt.timezone.utc).isoformat(),
                   "train_end":dt.datetime.fromtimestamp(train_end/1000,dt.timezone.utc).isoformat(),
                   "validation_end":dt.datetime.fromtimestamp(valid_end/1000,dt.timezone.utc).isoformat(),
                   "holdout_end":dt.datetime.fromtimestamp(end/1000,dt.timezone.utc).isoformat()},
        "symbols_based_on_historical_actual_51_100_trades":len(symbols),
        "analyzed_symbols":len(raw),
        "missing_symbols":errors,
        "trades_setup_events_by_family":{f:len(v) for f,v in by_family.items()},
        "exits":len(EXITS),
        "rule_and_exit_combinations_checked":checked,
        "profitable_train_and_validation_with_sample_threshold":len(candidates),
        "top_3_chosen_before_holdout":good,
        "other_train_validation_profitable_count":len(candidates),
        "baselines":baselines,
        "fitness_goal":"Genuinely net-positive 20 USDT per position, and holdout net positive with >=10 samples; no profitable claim unless sufficient evidence",
        "risk_note":"Selection bias: instruments chosen from actual future rank51..100 trades. 8-day 46-ish selected symbols, not precise historical all ranks. One open position globally with alphabetic same-minute order. Static 20 USDT, 0.12% RT fees, no spread/slippage/orderbook or LIVE exits. OHLC TP+SL same candle counted as SL, no intra-bar order sequencing. Searching many rules can overfit. Data-snooping means even a positive holdout is not proof. No real money orders. This script never touches LIVE strategy."
    }
    Path("volume_meaningful_profit_8day_report.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("MEANINGFUL_PROFIT_RESULT="+json.dumps(summary,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    main()
