"""READ-ONLY extended entry-indicator comparison for 37 SHORT wins vs 26 losses.

Signals sourced from prior completed, immutable 390-symbol public BloFin OHLCV shards.
No private API access or orders. Every feature computed from CLOSED candles prior
to the trade entry: RSI Wilder, MACD 12/26/9, Stochastic 14/3, ADX/DI,
ATR, OBV, VWAP, rolling volume 20/50, candle patterns, highs/lows,
15m/1h completed trend, BTC context. No historical order book/OI data.
"""
from __future__ import annotations
from collections import Counter
from itertools import combinations
from datetime import datetime, timezone
import json, math, statistics
from pathlib import Path
import short60_volume_mean10_24hour_readonly as base

F=base.FIVE
def med(a):
    a=[float(x) for x in a if x is not None and math.isfinite(float(x))]
    return round(statistics.median(a),5) if a else None
def isnum(x):
    return isinstance(x,(int,float)) and math.isfinite(float(x))
def ratio(a,b):
    return float(a)/float(b) if b is not None and b!=0 else None
def pc(a,b):
    return 100*(float(a)/float(b)-1) if b is not None and b!=0 else None
def avg(a):
    return sum(a)/len(a) if a else None
def ema_sequence(vals,n):
    if len(vals)<n:return []
    current=avg(vals[:n])
    out=[None]*(n-1)+[current]
    alpha=2/(n+1)
    for x in vals[n:]:
        current=alpha*x+(1-alpha)*current
        out.append(current)
    return out
def rsi_wilder(closes,n=14):
    if len(closes)<n+1:return None
    diffs=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    gain=avg([max(d,0) for d in diffs[:n]])
    loss=avg([max(-d,0) for d in diffs[:n]])
    for d in diffs[n:]:
        gain=(gain*(n-1)+max(d,0))/n
        loss=(loss*(n-1)+max(-d,0))/n
    if loss==0:return 100 if gain>0 else 50
    return 100-100/(1+gain/loss)
def macd(closes):
    fast=ema_sequence(closes,12)
    slow=ema_sequence(closes,26)
    valid=[]
    for j in range(len(closes)):
        if slow[j] is not None:
            valid.append(fast[j]-slow[j])
    sig=ema_sequence(valid,9)
    if len(sig)<2 or sig[-1] is None or sig[-2] is None:return (None,)*5
    line=valid[-1];signal=sig[-1];hist=line-signal
    prev=valid[-2]-sig[-2]
    return line,signal,hist,prev,hist-prev
def stochastic(bars,n=14):
    if len(bars)<n+4:return (None,)*3
    k=[]
    for last in range(n-1,len(bars)):
        seg=bars[last-n+1:last+1]
        lowest=min(c[2] for c in seg)
        highest=max(c[1] for c in seg)
        k.append(100*(seg[-1][3]-lowest)/(highest-lowest) if highest!=lowest else 50)
    smoothed=[avg(k[i-2:i+1]) for i in range(2,len(k))]
    if len(smoothed)<3:return (None,)*3
    kval=smoothed[-1]
    dval=avg(smoothed[-3:])
    return kval,dval,kval-dval
def adx(bars,n=14):
    if len(bars)<2*n+2:return (None,)*4
    trs=[];pls=[];mns=[]
    for i in range(1,len(bars)):
        prev=bars[i-1];cur=bars[i]
        hd=cur[1]-prev[1]
        ld=prev[2]-cur[2]
        pls.append(hd if hd>ld and hd>0 else 0)
        mns.append(ld if ld>hd and ld>0 else 0)
        trs.append(max(cur[1]-cur[2],abs(cur[1]-prev[3]),abs(cur[2]-prev[3])))
    r=sum(trs[:n]);p=sum(pls[:n]);m=sum(mns[:n])
    dx=[]
    for i in range(n-1,len(trs)):
        if i>=n:
            r=r-r/n+trs[i];p=p-p/n+pls[i];m=m-m/n+mns[i]
        di_plus=100*p/r if r else 0
        di_minus=100*m/r if r else 0
        dx.append(100*abs(di_plus-di_minus)/(di_plus+di_minus) if di_plus+di_minus else 0)
    if len(dx)<n:return (None,)*4
    value=avg(dx[:n]);prev=value
    for d in dx[n:]:
        prev=value
        value=(value*(n-1)+d)/n
    return value,di_plus,di_minus,value-prev

def completed_tf_return(rows, ts, units):
    block=F*units
    last_block_start=((ts+F)//block-1)*block
    curr=rows.get(last_block_start+(units-1)*F)
    before=rows.get(last_block_start-F)
    before2=rows.get(last_block_start-(units+1)*F)
    if curr is None or before is None:return None,None
    last=pc(curr[3],before[3])
    previous=pc(before[3],before2[3]) if before2 else None
    return last,previous

def features(t,rows,btcrows):
    ts=t["entry_ms"]-F
    last=180
    history=[rows.get(ts-i*F) for i in range(last-1,-1,-1)]
    if any(b is None for b in history):return None
    cur=history[-1]
    old=history[-2]
    p=history[-21:-1]
    closes=[b[3] for b in history]
    vols=[b[4] for b in history]
    high=cur[1];low=cur[2];o=cur[0];c=cur[3];v=cur[4]
    tr=[max(history[i][1]-history[i][2],
            abs(history[i][1]-history[i-1][3]),
            abs(history[i][2]-history[i-1][3])) for i in range(1,len(history))]
    atr14=avg(tr[-14:])
    v10=avg(vols[-11:-1])
    v20=avg(vols[-21:-1])
    v50=avg(vols[-51:-1])
    rsi=rsi_wilder(closes)
    prev_rsi=rsi_wilder(closes[:-1])
    line,signal,hist,prev_hist,hist_delta=macd(closes)
    k,d,k_gap=stochastic(history)
    adx14,di_plus,di_minus,adx_delta=adx(history)
    # OBV is a *change* over the historical lookback; absolute baseline is irrelevant.
    def obv(n):
        s=0
        for i in range(len(history)-n,len(history)):
            difference=closes[i]-closes[i-1]
            s+=vols[i]*(1 if difference>0 else -1 if difference<0 else 0)
        return s
    obv5=obv(5);obv10=obv(10);obv20=obv(20)
    typical=[(b[1]+b[2]+b[3])/3 for b in history]
    def vwap(n):
        numerator=sum(typical[-n+i]*vols[-n+i] for i in range(n))
        den=sum(vols[-n:])
        return numerator/den if den else None
    vw20=vwap(20)
    vw50=vwap(50)
    prev20=history[-21:-1]
    prev50=history[-51:-1]
    high20=max(b[1] for b in prev20)
    low20=min(b[2] for b in prev20)
    high50=max(b[1] for b in prev50)
    low50=min(b[2] for b in prev50)
    rng=high-low
    body=abs(o-c)
    last15,prev15=completed_tf_return(rows,ts,3)
    last60,prev60=completed_tf_return(rows,ts,12)
    btc0=btcrows.get(ts);btc3=btcrows.get(ts-3*F);btc12=btcrows.get(ts-12*F)
    br15=pc(btc0[3],btc3[3]) if btc0 and btc3 else None
    br60=pc(btc0[3],btc12[3]) if btc0 and btc12 else None
    self15=pc(c,history[-4][3])
    self60=pc(c,history[-13][3])
    cur_atr=100*atr14/c if c else None
    # Two non-overlapping, complete historical windows of size 10.
    recent10=history[-11:-1]
    older10=history[-21:-11]
    hi_recent=max(b[1] for b in recent10)
    lo_recent=min(b[2] for b in recent10)
    hi_older=max(b[1] for b in older10)
    lo_older=min(b[2] for b in older10)
    prev_closes=closes[-11:-1]
    prev_obv_10=obv(10)   # relative signed flow
    red_engulf=(c<o and old[3]>old[0] and o>=old[3] and c<=old[0])
    inside=(high<=old[1] and low>=old[2])
    pin_bear=(rng>0 and (high-max(o,c))/rng>=.5 and body/rng<=.35)
    doji=(rng>0 and body/rng<=.1)
    mean50=avg(vols[-51:-1])
    data={
      "vol_to_prev":ratio(v,old[4]),
      "vol_to_mean10":ratio(v,v10),
      "vol_to_mean20":ratio(v,v20),
      "vol_to_mean50":ratio(v,v50),
      "vol_mean10_to_mean50":ratio(v10,v50),
      "red_body_pct":100*(o-c)/o if o else None,
      "red_body_prev_green_ratio":ratio((o-c)/o,(old[3]-old[0])/old[0]) if o and old[0] else None,
      "body_range":body/rng if rng else None,
      "upper_wick_ratio":(high-max(o,c))/rng if rng else None,
      "lower_wick_ratio":(min(o,c)-low)/rng if rng else None,
      "red_engulfing":int(red_engulf),
      "inside_bar":int(inside),
      "bear_pin":int(pin_bear),
      "doji":int(doji),
      "rsi14":rsi,
      "rsi14_delta":rsi-prev_rsi if prev_rsi is not None and rsi is not None else None,
      "macd_line_pct":100*line/c if c and line is not None else None,
      "macd_hist_pct":100*hist/c if c and hist is not None else None,
      "macd_hist_delta_pct":100*hist_delta/c if c and hist_delta is not None else None,
      "macd_hist_rising":int(hist_delta>0) if hist_delta is not None else None,
      "macd_line_above_signal":int(line>signal) if line is not None else None,
      "stoch_k":k,
      "stoch_d":d,
      "stoch_k_minus_d":k_gap,
      "adx14":adx14,
      "adx_delta":adx_delta,
      "di_plus":di_plus,
      "di_minus":di_minus,
      "di_minus_above_plus":int(di_minus>di_plus) if di_minus is not None else None,
      "atr14_pct":cur_atr,
      "obv5_signed_to_mean20":ratio(obv5,v20),
      "obv10_signed_to_mean20":ratio(obv10,v20),
      "obv20_signed_to_mean20":ratio(obv20,v20),
      "vwap20_diff_pct":pc(c,vw20),
      "vwap50_diff_pct":pc(c,vw50),
      "distance_to_support20_pct":pc(c,low20),
      "distance_to_resistance20_pct":pc(high20,c),
      "distance_to_support50_pct":pc(c,low50),
      "distance_to_resistance50_pct":pc(high50,c),
      "new_20bar_low":int(low<low20),
      "new_20bar_high":int(high>high20),
      "new_50bar_low":int(low<low50),
      "new_50bar_high":int(high>high50),
      "higher_highs_last10":int(hi_recent>hi_older),
      "lower_lows_last10":int(lo_recent<lo_older),
      "lower_highs_last10":int(hi_recent<hi_older),
      "higher_lows_last10":int(lo_recent>lo_older),
      "completed_15m_change_pct":last15,
      "previous_15m_change_pct":prev15,
      "completed_1h_change_pct":last60,
      "previous_1h_change_pct":prev60,
      "btc_prior15_pct":br15,
      "btc_prior60_pct":br60,
      "coin_minus_btc_15m":self15-br15 if br15 is not None and self15 is not None else None,
      "coin_minus_btc_1h":self60-br60 if br60 is not None and self60 is not None else None,
      "price_prior15_pct":self15,
      "price_prior60_pct":self60,
      "price_prior150_pct":pc(c,history[-31][3]),
      "rsi_divergence_bullish_10bars":int(c<history[-11][3] and rsi is not None and rsi>rsi_wilder(closes[:-10])),
      "rsi_divergence_bearish_10bars":int(c>history[-11][3] and rsi is not None and rsi<rsi_wilder(closes[:-10])),
      "obv_bullish_divergence_10bars":int(c<history[-11][3] and obv10>0),
      "obv_bearish_divergence_10bars":int(c>history[-11][3] and obv10<0),
      "rank":t["rank"],
    }
    if any((z is not None and not isnum(z)) for z in data.values()):
        raise RuntimeError("Non-numeric feature unexpectedly found")
    return {"outcome":t["outcome"],"coin":t["inst"],"entry_ms":t["entry_ms"],**data}

def describe(t,keys):
    return {key:med([r.get(key) for r in t]) for key in keys}

def split(t,k,op,v):
    def pred(x):
        a=x.get(k)
        if a is None:return False
        return a>=v if op==">=" else a<=v
    matches=[x for x in t if pred(x)]
    c=Counter(x["outcome"] for x in matches)
    return {"wins":c["WIN"],"losses":c["LOSS"],
            "pct":round(100*c["WIN"]/(c["WIN"]+c["LOSS"]),2) if c["WIN"]+c["LOSS"] else None}

def mine(train,holdout):
    # Small, interpretable rules only: one or two pre-entry thresholds.
    win_mask=sum(1<<i for i,x in enumerate(train) if x["outcome"]=="WIN")
    loss_mask=((1<<len(train))-1)^win_mask
    criteria=[]
    for key in sorted(k for k in train[0] if k not in ("outcome","coin","entry_ms")):
        vals=sorted(set(float(r[key]) for r in train if r.get(key) is not None))
        if len(vals)<2:continue
        # thresholds entirely learned from training group
        cuts=sorted({vals[min(len(vals)-1,round((len(vals)-1)*q/100))] for q in (10,20,30,40,50,60,70,80,90)})
        for q in cuts:
            for op in (">=","<="):
                mask=sum(1<<i for i,r in enumerate(train) if r.get(key) is not None and
                         (r[key]>=q if op==">=" else r[key]<=q))
                nwin=(mask&win_mask).bit_count()
                if nwin<5:continue
                criteria.append((key,op,q,mask,nwin,(mask&loss_mask).bit_count()))
    criteria.sort(key=lambda x:(x[0],x[1],x[2]))
    # Max N and variant count, avoid explosion or tuning to holdout
    output=[]
    def add_group(group,mask):
        nw=(mask&win_mask).bit_count()
        nl=(mask&loss_mask).bit_count()
        if nw<5 or nl!=0:return
        check=holdout
        chosen=[r for r in check if all(
            r.get(key) is not None and (r[key]>=q if op==">=" else r[key]<=q)
            for key,op,q,*_ in group)]
        wc=Counter(r["outcome"] for r in chosen)
        output.append({"criteria":[f"{key} {op} {q:.5g}" for key,op,q,*_ in group],
                       "train_wins":nw,"train_losses":0,
                       "holdout_wins":wc["WIN"],"holdout_losses":wc["LOSS"],
                       "holdout_success_pct":round(100*wc["WIN"]/(wc["WIN"]+wc["LOSS"]),2)
                             if wc["WIN"]+wc["LOSS"] else None})
    for a in criteria:
        add_group([a],a[3])
    for i,a in enumerate(criteria):
        for b in criteria[i+1:]:
            if a[0]==b[0]:continue
            mask=a[3]&b[3]
            if (mask&win_mask).bit_count()>=5 and not(mask&loss_mask):
                add_group([a,b],mask)
    output.sort(key=lambda x:(-x["train_wins"],len(x["criteria"])))
    return len(criteria),len(output),output[:25]

def main():
    data,errors,total=base.load()
    assert len(data)==390 and not errors
    ranks,coverage=base.rankings(data)
    res=base.test_period(data,ranks,base.START,base.END,1.6)
    assert (res["wins"],res["losses"],res["flat"])==(528,649,21)
    btc_coin=next((x for x in data if x in ("BTC-USDT","BTCUSDT","BTC-USDT-SWAP","BTC-USDT-PERP")),None)
    if btc_coin is None:
        matches=[x for x in data if x.startswith("BTC-") and "USDT" in x]
        if matches:btc_coin=matches[0]
    if btc_coin is None:raise RuntimeError("BTC market missing; cannot test BTC context")
    btcrows=data[btc_coin]
    alltrades=[]
    skips=0
    for t in res["trades"]:
        if t["outcome"] not in ("WIN","LOSS"):continue
        f=features(t,data[t["inst"]],btcrows)
        if f is None:
            skips+=1
            continue
        alltrades.append(f)
    group=[f for f in alltrades if f["vol_to_prev"] is not None and
           3<=f["vol_to_prev"]<4 and f["price_prior150_pct"]>=0]
    g=Counter(r["outcome"] for r in group)
    print("FEATURE_INPUT_REPRODUCED="+json.dumps({
          "btc_coin":btc_coin,"all_24h_analyzed":len(alltrades),"skipped":skips,
          "group_wins":g["WIN"],"group_losses":g["LOSS"]}),flush=True)
    assert (g["WIN"],g["LOSS"])==(37,26),(g["WIN"],g["LOSS"])
    others=[f for f in alltrades if f not in group]
    win=[r for r in group if r["outcome"]=="WIN"]
    loss=[r for r in group if r["outcome"]=="LOSS"]
    keys=[k for k in group[0] if k not in ("outcome","coin","entry_ms")]
    compare={key:{"win_median":med([r.get(key) for r in win]),
                  "loss_median":med([r.get(key) for r in loss])} for key in keys}
    # Pre-specified conventional cutoffs, no optimal threshold tuning.
    checks=[
      ("MACD_hist_negative","macd_hist_pct","<=",0),
      ("MACD_hist_declining","macd_hist_delta_pct","<=",0),
      ("MACD_line_below_signal","macd_line_above_signal","<=",0),
      ("ADX_over25","adx14",">=",25),
      ("ADX_over38","adx14",">=",38),
      ("DI_minus_above_plus","di_minus_above_plus",">=",1),
      ("OBV_5_falling","obv5_signed_to_mean20","<=",0),
      ("OBV_10_falling","obv10_signed_to_mean20","<=",0),
      ("STOCH_below20","stoch_k","<=",20),
      ("STOCH_below50","stoch_k","<=",50),
      ("STOCH_K_below_D","stoch_k_minus_d","<=",0),
      ("VWAP20_price_below","vwap20_diff_pct","<=",0),
      ("ATR14_over0p5pct","atr14_pct",">=",.5),
      ("Volume_more_than_half_mean20","vol_to_mean20",">=",.5),
      ("Volume_below_mean50","vol_to_mean50","<=",1),
      ("Bear_engulfing","red_engulfing",">=",1),
      ("Lower_low20","new_20bar_low",">=",1),
      ("Price_near_support20_0p2pct","distance_to_support20_pct","<=",.2),
      ("Price_near_resistance20_0p2pct","distance_to_resistance20_pct","<=",.2),
      ("Falling_completed_15m","completed_15m_change_pct","<=",0),
      ("Falling_completed_1h","completed_1h_change_pct","<=",0),
      ("BTC_falling_last1h","btc_prior60_pct","<=",0),
      ("BTC_falling_last15m","btc_prior15_pct","<=",0),
      ("Coin_underperforming_BTC_1h","coin_minus_btc_1h","<=",0),
      ("Bullish_RSI_divergence","rsi_divergence_bullish_10bars",">=",1),
      ("Bearish_RSI_divergence","rsi_divergence_bearish_10bars",">=",1),
      ("Falling_MACD_in_downtrend", "macd_hist_delta_pct","<=",0),
    ]
    standard=[]
    for name,key,op,value in checks:
        result=split(group,key,op,value)
        oth=split(others,key,op,value)
        standard.append({"name":name,"key":key,"compare":op,"cutoff":value,
                         "group":result,"other_trades":oth})
    standard.sort(key=lambda x:(-(x["group"]["pct"] or 0),-x["group"]["wins"]))
    count,pats,top=mine(group,others)
    out={
      "period_uk":[base.clock(base.START),base.clock(base.END)],
      "data_coins":len(data),"btc_reference":btc_coin,
      "cohort":"37 profitable SHORT vs 26 losing; vol ratio 3x-4x previous green, <= mean10 of previous 10, earlier 2.5h price change nonnegative",
      "group_wins":g["WIN"],"group_losses":g["LOSS"],
      "remaining_trades":{"wins":sum(t["outcome"]=="WIN" for t in others),
                           "losses":sum(t["outcome"]=="LOSS" for t in others)},
      "indicators_evaluated":len(keys),
      "indicator_medians":compare,
      "conventional_threshold_checks":standard,
      "searched_cutoffs":count,
      "patterns_zero_loss_in_training":pats,
      "best_training_zero_loss_patterns_and_other_trades":top,
      "not_evaluable":["Historical order book snapshots","Historical Open Interest series"],
      "caveats":"Retrospective in-sample search is severely overfit, no guarantee. Other trades in same 24h are a contrast cohort, not independent future validation. All inputs use closed candles before entry; BTC state is contemporaneous. Simulation ignores spread, slippage and funding. Independent trades can overlap. No LIVE edits."
    }
    Path("short37_extra_indicators_readonly_report.json").write_text(
        json.dumps(out,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("EXTRA_INDICATOR_SUMMARY="+json.dumps({
        "period_uk":out["period_uk"],
        "data_coins":out["data_coins"],
        "btc_reference":btc_coin,
        "group_wins":g["WIN"],"group_losses":g["LOSS"],
        "remaining_trades":out["remaining_trades"],
        "indicators_evaluated":len(keys),
        "conventional_checks":standard,
        "searched_cutoffs":count,
        "patterns_zero_loss_in_training":pats,
        "top_patterns":top[:12],
        "not_evaluable":out["not_evaluable"]
    },separators=(",",":"),ensure_ascii=False),flush=True)
    print("REPORT_WRITTEN=short37_extra_indicators_readonly_report.json",flush=True)

if __name__=="__main__":main()
