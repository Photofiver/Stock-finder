"""READ ONLY: test volume regime as actual position-holding rule.

Trend is user-specified: strongest same-color 5m volume candle; opposite
color must strictly exceed it to flip. Enter on next open after candidate,
remain open UNTIL first of: opposite trend flip, broker-like TP/SL, max age.
Trend-flip exit is next bar OPEN, to avoid lookahead from close-only signals.
Train=first 4 days, validate=next 2 days, untouched holdout=last 2 days.
Uses publicly available OHLCV and no LIVE broker credentials.
"""
from __future__ import annotations
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timezone
from itertools import product
from pathlib import Path
import json
import statistics
import time

import blofin_volume_meaningful_profit_8day as previous
import blofin_volume_peak_trend_backtest_readonly as vol

FIVE=300000
FEE=0.12
NOTIONAL=20.
TP_LEVELS=(0.01,0.02,0.04,None)
SL_LEVELS=(0.005,0.01,0.02)
HOLDS=(6,12,24,48,96)  # 30min, 1h, 2h, 4h, 8h
EXITS=list(product(TP_LEVELS,SL_LEVELS,HOLDS))
VOL_THRESHOLDS=(0.75,1.5,2.5)
BODY_THRESHOLDS=(0.,0.25)
SIDES=("BOTH","LONG","SHORT")
FAMILIES=("FLIP","EARLY","PEAK","PULLBACK","BREAKOUT","SMA_CROSS")
MIN_TRAIN=10
MIN_VALID=5


def rsi_proxy(bars,i,side):
    closes=[x["c"] for x in bars[i-14:i+1]]
    ups=[max(0,closes[k]-closes[k-1]) for k in range(1,len(closes))]
    downs=[max(0,closes[k-1]-closes[k]) for k in range(1,len(closes))]
    g=sum(ups);d=sum(downs)
    return g>=d if side=="LONG" else d>=g


def collect(inst,chunks,start,end):
    events=[]
    for bars in chunks:
        regimes=vol.regime_series(bars)
        age=0
        last_regime=None
        for i in range(35,len(bars)-3):
            b=bars[i]
            at=b["ts"]+FIVE
            side,flip,_=regimes[i]
            age=0 if side!=last_regime else age+1
            last_regime=side
            if not(start<=at<end and side):
                continue
            color=vol.classify_candle(b)
            if b["o"]<=0:
                continue
            body=abs(b["c"]/b["o"]-1)*100
            med=statistics.median(x["v"] for x in bars[i-20:i])
            if med<=0:continue
            ratio=b["v"]/med
            prev=bars[i-1]
            prev_color=vol.classify_candle(prev)
            closes=[x["c"] for x in bars[i-20:i+1]]
            m10=sum(closes[-10:])/10
            m20=sum(closes[-20:])/20
            p10=sum(closes[-11:-1])/10
            p20=sum(closes[-21:-1])/20
            cross=("LONG" if p10<=p20 and m10>m20 else
                   "SHORT" if p10>=p20 and m10<m20 else None)
            breakout=(b["c"]>max(x["h"] for x in bars[i-12:i]) if side=="LONG"
                      else b["c"]<min(x["l"] for x in bars[i-12:i]))
            fam=[]
            if flip:
                fam.append("FLIP")
            if age<=3 and color==side and b["v"]>prev["v"]:
                fam.append("EARLY")
            if color==side and b["v"]>regimes[i-1][2] and not flip:
                fam.append("PEAK")
            if color==side and prev_color!=side and b["v"]>prev["v"]:
                fam.append("PULLBACK")
            if color==side and breakout:
                fam.append("BREAKOUT")
            if cross==side:
                fam.append("SMA_CROSS")
            if not fam:
                continue
            signals=[]
            for cfgid,(tp,sl,hold) in enumerate(EXITS):
                x=simulate(bars,regimes,i,side,tp,sl,hold)
                if x is not None:
                    signals.append((cfgid,x))
            if not signals:continue
            for family in fam:
                events.append({"at":at,"inst":inst,"family":family,"side":side,
                               "vol":ratio,"body":body,
                               "ma_align":(m10>m20 if side=="LONG" else m10<m20),
                               "rsi_proxy_align":rsi_proxy(bars,i,side),
                               "outcomes":dict(signals)})
    return events


def simulate(bars,regimes,i,side,tp,sl,hold):
    entry=bars[i+1]["o"]
    if entry<=0:return None
    for step in range(1,hold+1):
        j=i+step
        if j>=len(bars):
            return None  # unknown outcome
        bar=bars[j]
        if side=="LONG":
            stop=bar["l"]<=entry*(1-sl)
            take=bar["h"]>=entry*(1+tp) if tp is not None else False
        else:
            stop=bar["h"]>=entry*(1+sl)
            take=bar["l"]<=entry*(1-tp) if tp is not None else False
        if stop:
            return (-100*sl,bar["ts"]+FIVE,"SL" if not take else "BOTH_UNKNOWN")
        if take:
            return (100*tp,bar["ts"]+FIVE,"TP")
        # The trend signal is known only once *current candle closes*.
        # At that time, the next candle's OPEN is first usable exit price.
        if regimes[j][0] != side and j+1<len(bars):
            px=bars[j+1]["o"]
            return (100*(px/entry-1)*(1 if side=="LONG" else -1),
                    bars[j+1]["ts"],"TREND_FLIP")
    j=i+hold
    return (100*(bars[j]["c"]/entry-1)*(1 if side=="LONG" else -1),
            bars[j]["ts"]+FIVE,"MAX_HOLD")


def eval_rule(events,config):
    busy=-1
    pnl=[]
    why={}
    for e in events:
        if e["at"]<busy:continue
        result=e["outcomes"].get(config)
        if result is None:continue
        change,busy,reason=result
        pnl.append(change)
        why[reason]=why.get(reason,0)+1
    n=len(pnl)
    gross=sum(pnl)*NOTIONAL/100
    fees=n*NOTIONAL*FEE/100
    return {"trades":n,"wins":sum(p>0 for p in pnl),
            "net_win_count":sum(p>FEE for p in pnl),
            "gross_usdt":round(gross,4),"estimated_fees":round(fees,4),
            "net_usdt":round(gross-fees,4),
            "net_pct_per_trade":round(sum(pnl)/n-FEE,4) if n else None,
            "exit_reasons":why}


def get_matching(familyevents,rule,lower,upper):
    _,volmin,bodymin,side,ma_mode,rsi_mode=rule
    return [e for e in familyevents
            if lower<=e["at"]<upper and e["vol"]>=volmin
            and e["body"]>=bodymin
            and (side=="BOTH" or e["side"]==side)
            and (ma_mode=="ANY" or e["ma_align"])
            and (rsi_mode=="ANY" or e["rsi_proxy_align"])]


def rule_details(rule,exitidx):
    family,volmin,bodymin,side,ma_mode,rsi_mode=rule
    tp,sl,hold=EXITS[exitidx]
    return {"family":family,"volume_over_prior20_median":volmin,
            "min_candle_body_pct":bodymin,"side":side,
            "ma_filter":ma_mode,"rsi_direction_proxy":rsi_mode,
            "tp_pct":100*tp if tp is not None else None,
            "sl_pct":100*sl,"max_hold_minutes":hold*5,
            "exit_at_opposite_volume_trend":True}


def main():
    state=json.loads(Path("blofin_live_state.json").read_text())
    end=(int(time.time()*1000)//FIVE)*FIVE
    start=end-8*86400000
    mid=start+4*86400000
    validation_end=mid+2*86400000
    insts=[]
    for t in state.get("trade_history",[]):
        try:
            rank=int(t.get("signal_rank") or 0)
            ts=int(t.get("opened_ms") or 0)
        except (TypeError,ValueError,AttributeError):continue
        inst=t.get("inst")
        if 51<=rank<=100 and start<=ts<end and inst and inst not in insts:
            insts.append(inst)
    data={}
    errors={}
    t0=time.monotonic()
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures={pool.submit(previous.fetch_8days,inst,end,start-150*FIVE):inst for inst in insts}
        for fut in as_completed(futures):
            inst=futures[fut]
            try:
                chunks=fut.result()
                if chunks:data[inst]=chunks
                else:errors[inst]="No contiguous candles"
            except Exception as exc:errors[inst]=str(exc)
    print(f"CANDLE_FETCHED={len(data)} ERRORS={len(errors)} FETCH_SEC={time.monotonic()-t0:.1f}",flush=True)
    if len(data)<10:raise RuntimeError("Too few candles")
    events=[]
    for inst,chunks in data.items():
        events.extend(collect(inst,chunks,start,end))
    by_fam={f:[] for f in FAMILIES}
    for e in events:by_fam[e["family"]].append(e)
    for fam in FAMILIES:
        by_fam[fam].sort(key=lambda e:(e["at"],e["inst"]))
    print(f"CANDIDATE_EVENTS={len(events)} EXITS={len(EXITS)} FAMILY_COUNTS={[(k,len(v)) for k,v in by_fam.items()]}",flush=True)

    candidates=[]
    checked=0
    for fam in FAMILIES:
        entries=by_fam[fam]
        for volmin,bodymin,side,ma_mode,rsi_mode in product(VOL_THRESHOLDS,BODY_THRESHOLDS,SIDES,("ANY","ALIGNED"),("ANY","ALIGNED")):
            rule=(fam,volmin,bodymin,side,ma_mode,rsi_mode)
            training=get_matching(entries,rule,start,mid)
            validating=get_matching(entries,rule,mid,validation_end)
            if len(training)<MIN_TRAIN or len(validating)<MIN_VALID:continue
            for exitidx in range(len(EXITS)):
                checked+=1
                tr=eval_rule(training,exitidx)
                if tr["trades"]<MIN_TRAIN or tr["net_usdt"]<=0:continue
                va=eval_rule(validating,exitidx)
                if va["trades"]<MIN_VALID or va["net_usdt"]<=0:continue
                # Ranking from first 6d ONLY, no holdout in selection
                metric=min(tr["net_usdt"]*.5,va["net_usdt"])
                candidates.append({"rule":rule,"cfgid":exitidx,"train":tr,"validation":va,"rank":metric})
    candidates.sort(key=lambda c:(c["rank"],c["train"]["net_usdt"]+c["validation"]["net_usdt"]),reverse=True)
    print(f"SCENARIOS_CHECKED={checked} VALIDATED_POSITIVE={len(candidates)}",flush=True)
    top=[]
    for c in candidates[:3]:
        rule=c["rule"]
        hold=get_matching(by_fam[rule[0]],rule,validation_end,end)
        ho=eval_rule(hold,c["cfgid"])
        top.append({**rule_details(rule,c["cfgid"]),"train":c["train"],
                    "validation":c["validation"],"untouched_holdout":ho})
    baselines={}
    for family in ("FLIP","EARLY","BREAKOUT","SMA_CROSS"):
        rule=(family,0.75,0.,"BOTH","ANY","ANY")
        for cfg in ((0.02,0.01,24),(0.04,0.02,48),(None,0.02,48)):
            idx=EXITS.index(cfg)
            name=f"{family}_TP{cfg[0]}_SL{cfg[1]}_MAX{cfg[2]*5}m"
            baselines[name]={"train":eval_rule(get_matching(by_fam[family],rule,start,mid),idx),
                             "validation":eval_rule(get_matching(by_fam[family],rule,mid,validation_end),idx),
                             "holdout":eval_rule(get_matching(by_fam[family],rule,validation_end,end),idx)}
    result={
        "asof_utc":datetime.now(timezone.utc).isoformat(),
        "window":{"from":datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),
                  "training_end":datetime.fromtimestamp(mid/1000,timezone.utc).isoformat(),
                  "validation_end":datetime.fromtimestamp(validation_end/1000,timezone.utc).isoformat(),
                  "untouched_end":datetime.fromtimestamp(end/1000,timezone.utc).isoformat()},
        "symbols_requested":len(insts),"symbols_analyzed":len(data),"missing":errors,
        "family_events":{f:len(v) for f,v in by_fam.items()},
        "configurations_examined_train_plus_validation":checked,
        "positive_train_and_validation":len(candidates),
        "top_selected_before_holdout":top,
        "baselines":baselines,
        "notes":"Original user's max opposite volume regime, confirmed closed 5m candles. Hold until trend reversal, TP, SL, or time. Fees estimated 0.12% RT, notional 20 USDT/trade. Alphabetic tie selection is not LIVE ranking. Ex-post coin selection from LIVE history; no spread/slippage/orderbook; TP+SL same candle => conservative SL. High risk of overfitting across many variants. No changes to LIVE trading code."
    }
    Path("volume_hold_until_reversal_report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print("VOLUME_HOLD_RESULT="+json.dumps(result,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    main()
