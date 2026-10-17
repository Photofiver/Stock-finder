"""Read-only PULLBACK winners vs losers on exactly the original 8-day window.
Never places trades or accesses private API keys. Uses same historic coin universe,
5m volume regime and one open position globally as 2026-10-17 original test.
"""
import json, math, statistics, time
from collections import Counter,defaultdict
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from itertools import combinations
import blofin_volume_hold_until_reversal_readonly as base
import blofin_volume_meaningful_profit_8day as previous
import blofin_volume_peak_trend_backtest_readonly as vol

FIVE=300000
START=int(datetime(2026,10,9,17,20,tzinfo=timezone.utc).timestamp()*1000)
TRAIN=START+4*86400000
VALID=TRAIN+2*86400000
END=VALID+2*86400000
NOTIONAL=20.0
FEE=0.12

def median(z):
    return round(statistics.median(z),4) if z else None

def ema(xs,span):
    a=2/(span+1);o=[];prev=None
    for x in xs:
        prev=x if prev is None else (a*x+(1-a)*prev)
        o.append(prev)
    return o

def rsi(xs,n=14):
    up=dn=0.0
    for k in range(1,n+1):
        d=xs[k]-xs[k-1]
        up+=max(0,d); dn+=max(0,-d)
    up/=n;dn/=n
    v=[None]*len(xs)
    v[n]=100-100/(1+up/dn) if dn else 100.
    for i in range(n+1,len(xs)):
        d=xs[i]-xs[i-1]
        up=(up*(n-1)+max(0,d))/n
        dn=(dn*(n-1)+max(0,-d))/n
        v[i]=100-100/(1+up/dn) if dn else 100.
    return v

def get_indicators(bars):
    closes=[b["c"] for b in bars]
    volumes=[b["v"] for b in bars]
    ma10=[None if i<9 else sum(closes[i-9:i+1])/10 for i in range(len(bars))]
    ma20=[None if i<19 else sum(closes[i-19:i+1])/20 for i in range(len(bars))]
    e12=ema(closes,12);e26=ema(closes,26)
    macd=[x-y for x,y in zip(e12,e26)]
    macds=ema(macd,9)
    mh=[a-b for a,b in zip(macd,macds)]
    rr=rsi(closes)
    kk=[]
    for i,b in enumerate(bars):
        if i<13:kk.append(None);continue
        hi=max(x["h"] for x in bars[i-13:i+1])
        lo=min(x["l"] for x in bars[i-13:i+1])
        kk.append(100*(b["c"]-lo)/(hi-lo) if hi>lo else 50.0)
    obv=[0.0]
    for i in range(1,len(bars)):
        obv.append(obv[-1]+(volumes[i] if closes[i]>closes[i-1] else (-volumes[i] if closes[i]<closes[i-1] else 0)))
    return ma10,ma20,mh,rr,kk,obv

def features(bars,i,dir,regimes,age,ind):
    b=bars[i]; prev=bars[i-1]
    ma10,ma20,mh,rr,kk,obv=ind
    sign=1 if dir=="LONG" else -1
    med20=statistics.median(x["v"] for x in bars[i-20:i])
    body=abs(b["c"]/b["o"]-1)*100
    orient=lambda z:sign*z
    high12=max(x["h"] for x in bars[i-12:i])
    low12=min(x["l"] for x in bars[i-12:i])
    candle_range=(b["h"]-b["l"])/b["o"]*100
    rv=rr[i] if rr[i] is not None else 50
    return {
      "side":dir,
      "vol_ratio":b["v"]/med20 if med20 else None,
      "vol_vs_opposite":b["v"]/prev["v"] if prev["v"]>0 else None,
      "body_pct":body,
      "body_share":body/candle_range if candle_range>0 else 0,
      "age":age,
      "rsi":rv,
      "rsi_aligned":rv>=50 if dir=="LONG" else rv<=50,
      "rsi_extreme":rv>70 if dir=="LONG" else rv<30,
      "ma_aligned":ma10[i]>ma20[i] if dir=="LONG" else ma10[i]<ma20[i],
      "ma10_slope_pct":orient((ma10[i]/ma10[i-3]-1)*100),
      "ma20_slope_pct":orient((ma20[i]/ma20[i-3]-1)*100),
      "ma_gap_pct":orient((ma10[i]/ma20[i]-1)*100),
      "distance_ma10_pct":orient((b["c"]/ma10[i]-1)*100),
      "macd_hist_oriented":orient(mh[i]/b["c"]*100),
      "macd_hist_rising":orient(mh[i]-mh[i-1])>0,
      "macd_hist_accelerating":orient(mh[i]-mh[i-1])>orient(mh[i-1]-mh[i-2]),
      "stoch_aligned":kk[i]>kk[i-1] if dir=="LONG" else kk[i]<kk[i-1],
      "stoch_overbought":kk[i]>80 if dir=="LONG" else kk[i]<20,
      "obv_slope5":orient(obv[i]-obv[i-5])/max(sum(x["v"] for x in bars[i-5:i+1]),1),
      "breakout12":b["c"]>high12 if dir=="LONG" else b["c"]<low12,
      "prev_body_pct":abs(prev["c"]/prev["o"]-1)*100,
      "prev_vol_ratio":prev["v"]/med20 if med20 else None,
      "hour_uk":int(datetime.fromtimestamp((b["ts"]+FIVE)/1000,timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Europe/London')).hour),
    }

def collect(inst,chunks):
    out=[]
    for bars in chunks:
        regimes=vol.regime_series(bars)
        ind=get_indicators(bars)
        age=0; last=None
        for i in range(35,len(bars)-3):
            b=bars[i]; at=b["ts"]+FIVE
            side,flip,_=regimes[i]
            age=0 if side!=last else age+1
            last=side
            if not(START<=at<END and side):
                continue
            color=vol.classify_candle(b)
            prev=bars[i-1]
            if (b["o"]<=0 or color!=side or
                vol.classify_candle(prev)==side or b["v"]<=prev["v"]):
                continue
            body=abs(b["c"]/b["o"]-1)*100
            if body<.25:continue
            med=statistics.median(x["v"] for x in bars[i-20:i])
            if med<=0 or b["v"]/med<2.5:continue
            outcome=base.simulate(bars,regimes,i,side,None,.005,12)
            if outcome is None:continue
            pnl,closems,why=outcome
            out.append({"at":at,"inst":inst,"side":side,"closems":closems,"reason":why,
                        "gross_pct":pnl,"net_pct":pnl-FEE,
                        "features":features(bars,i,side,regimes,age,ind)})
    return out

def select(entries,lo,hi):
    busy=-1;chosen=[]
    for e in entries:
        if not (lo<=e["at"]<hi) or e["at"]<busy:continue
        busy=e["closems"];chosen.append(e)
    return chosen

def metrics(xs):
    n=len(xs)
    net=sum(x["net_pct"] for x in xs)*NOTIONAL/100
    days=defaultdict(list)
    for x in xs:
        d=datetime.fromtimestamp(x["at"]/1000,timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Europe/London')).strftime("%Y-%m-%d")
        days[d].append(x)
    return {"n":n,"net_wins":sum(x["net_pct"]>0 for x in xs),
            "wins_pct":round(100*sum(x["net_pct"]>0 for x in xs)/n,1) if n else None,
            "net_usdt":round(net,4),
            "net_per_trade_pct":round(net*100/NOTIONAL/n,4) if n else None,
            "days":{d:{"n":len(z),"net_usdt":round(sum(x["net_pct"] for x in z)*NOTIONAL/100,4)} for d,z in sorted(days.items())}}

def filter_rules():
    rules=[]
    def add(name,fun):
        rules.append((name,fun))
    for key in ("side","rsi_aligned","rsi_extreme","ma_aligned","macd_hist_rising","macd_hist_accelerating",
                "stoch_aligned","stoch_overbought","breakout12"):
        vals=("LONG","SHORT") if key=="side" else (True,False)
        for v in vals:
            add(key+"="+str(v),lambda e,k=key,val=v:e["features"][k]==val)
    numeric={
      "vol_ratio":[2.5,3,4,5,7,8,10,12,15,20,30],
      "vol_vs_opposite":[1.0,1.5,2,3,4,8,12,16,24],
      "body_pct":[.25,.4,.6,.8,1.],
      "body_share":[.35,.5,.7,.85],
      "age":[3,6,12,24,48,96],
      "ma10_slope_pct":[-.1,0,.03,.07,.12,.2],
      "ma20_slope_pct":[-.08,0,.03,.07,.12],
      "ma_gap_pct":[-.3,0,.05,.1,.2,.4],
      "distance_ma10_pct":[-.3,0,.1,.2,.3,.4,.5,.7],
      "macd_hist_oriented":[-.03,0,.03,.06,.1],
      "obv_slope5":[-.5,-.2,0,.2,.5],
      "prev_body_pct":[.05,.15,.3,.5],
      "prev_vol_ratio":[.5,1.,2.,3.],
      "rsi":[30,40,45,50,55,60,70],
    }
    for key,limits in numeric.items():
        for value in limits:
            add(key+">="+str(value),lambda e,k=key,t=value:e["features"][k] is not None and e["features"][k]>=t)
            add(key+"<="+str(value),lambda e,k=key,t=value:e["features"][k] is not None and e["features"][k]<=t)
    for a,b in [(0,6),(6,12),(12,18),(18,24)]:
        add("uk_hour="+str(a)+"-"+str(b),lambda e,aa=a,bb=b:aa<=e["features"]["hour_uk"]<bb)
    return rules

def main():
    state=json.loads(Path("blofin_live_state.json").read_text())
    insts=[]
    for t in state.get("trade_history",[]):
        try:
            rank=int(t.get("signal_rank") or 0)
            ts=int(t.get("opened_ms") or 0)
        except (TypeError,ValueError,AttributeError):continue
        inst=t.get("inst")
        if 51<=rank<=100 and START<=ts<END and inst and inst not in insts:
            insts.append(inst)
    data={};errors={}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures={pool.submit(previous.fetch_8days,inst,END,START-150*FIVE):inst for inst in insts}
        for fut in as_completed(futures):
            inst=futures[fut]
            try:
                chunks=fut.result()
                if chunks:data[inst]=chunks
                else:errors[inst]="NO_CONTIGUOUS"
            except Exception as exc: errors[inst]=str(exc)
    print("FETCH_COUNT="+json.dumps({"requested":len(insts),"fetched":len(data),"errors":errors}),flush=True)
    if len(data)<25:
        raise RuntimeError("Too few historical symbols")
    entries=[]
    for inst,chunks in data.items():entries.extend(collect(inst,chunks))
    entries.sort(key=lambda e:(e["at"],e["inst"]))
    windows=[select(entries,START,TRAIN),select(entries,TRAIN,VALID),select(entries,VALID,END)]
    base_stats=[metrics(w) for w in windows]
    selected=[x for w in windows for x in w]
    baseline=metrics(selected)
    print("BASELINE="+json.dumps({"windows":base_stats,"overall":baseline},separators=(",",":")),flush=True)
    expected=(124,62,60)
    reproducible=tuple(len(x) for x in windows)==expected
    if not reproducible:
        print("WARNING_MISMATCH_EXPECTED="+json.dumps({"expected":expected,"actual":[len(w) for w in windows]}),flush=True)
    wins=[x for x in selected if x["net_pct"]>0]
    notwins=[x for x in selected if x["net_pct"]<=0]
    fkeys=("vol_ratio","vol_vs_opposite","body_pct","body_share","age","rsi","ma10_slope_pct",
           "ma20_slope_pct","ma_gap_pct","distance_ma10_pct","macd_hist_oriented","obv_slope5",
           "prev_body_pct","prev_vol_ratio")
    differences={}
    for key in fkeys:
        differences[key]={"win_median":median([e["features"][key] for e in wins if e["features"][key] is not None]),
                         "nonwin_median":median([e["features"][key] for e in notwins if e["features"][key] is not None])}
    print("WINNER_DIFFERENCES="+json.dumps(differences,separators=(",",":")),flush=True)
    candidates=[]
    rules=filter_rules()
    t,v,h=windows
    for name,rule in rules:
        tt=[x for x in t if rule(x)]
        vv=[x for x in v if rule(x)]
        if len(tt)<14 or len(vv)<7:continue
        a=metrics(tt);b=metrics(vv)
        if a["net_usdt"]<=0 or b["net_usdt"]<=0:continue
        if a["net_per_trade_pct"]<=base_stats[0]["net_per_trade_pct"] or b["net_per_trade_pct"]<=base_stats[1]["net_per_trade_pct"]:continue
        score=min(a["net_per_trade_pct"]-base_stats[0]["net_per_trade_pct"],b["net_per_trade_pct"]-base_stats[1]["net_per_trade_pct"])
        candidates.append((score,name,rule,a,b))
    candidates.sort(key=lambda a:(a[0],min(a[3]["n"],a[4]["n"])),reverse=True)
    top=[]
    for score,name,rule,tr,va in candidates[:15]:
        he=metrics([x for x in h if rule(x)])
        allsel=metrics([x for x in selected if rule(x)])
        top.append({"rule":name,"train":{k:v for k,v in tr.items() if k!="days"},"validation":{k:v for k,v in va.items() if k!="days"},
                    "holdout":{k:v for k,v in he.items() if k!="days"},
                    "all":{k:v for k,v in allsel.items() if k!="days"},"score":round(score,4)})
    print("TOP_TRAIN_VALID_FILTERS="+json.dumps({"n_rules":len(rules),"qualifying":len(candidates),"top":top},separators=(",",":")),flush=True)
    cats={}
    for k in ("side","ma_aligned","rsi_aligned","macd_hist_rising","macd_hist_accelerating",
              "stoch_aligned","breakout12","rsi_extreme"):
        cats[k]={str(v):{q:p for q,p in metrics([e for e in selected if e["features"][k]==v]).items() if q!="days"}
                 for v in (("LONG","SHORT") if k=="side" else (True,False))}
    print("CATEGORIES="+json.dumps(cats,separators=(",",":")),flush=True)

    # Predefined interpretability comparisons: same historical trades, without opening replacement trades.
    focused={
        "SHORT":[("side","SHORT")],
        "LONG":[("side","LONG")],
        "SHORT_and_vol_le10":[("side","SHORT"),("vol_ratio","<=10")],
        "SHORT_and_vol_le12":[("side","SHORT"),("vol_ratio","<=12")],
        "SHORT_and_ma10_distance_le0.3":[("side","SHORT"),("distance_ma10_pct","<=0.3")],
        "SHORT_and_not_12bar_breakout":[("side","SHORT"),("breakout12",False)],
        "SHORT_and_macd_falling":[("side","SHORT"),("macd_hist_rising",False)],
        "vol_le10":[("vol_ratio","<=10")],
        "vol_le12":[("vol_ratio","<=12")],
        "vol_le15":[("vol_ratio","<=15")],
        "vol_le20":[("vol_ratio","<=20")],
        "near_ma10":[("distance_ma10_pct","<=0.3")],
        "ma10_and_low_volume":[("distance_ma10_pct","<=0.3"),("vol_ratio","<=12")],
        "non_breakout_low_volume":[("breakout12",False),("vol_ratio","<=12")],
        "macd_not_rising":[("macd_hist_rising",False)],
        "macd_not_rising_low_volume":[("macd_hist_rising",False),("vol_ratio","<=12")],
        "12_18_uk":[("hour_uk",">=12"),("hour_uk","<18")],
        "12_18_uk_SHORT":[("hour_uk",">=12"),("hour_uk","<18"),("side","SHORT")],
        "12_18_uk_lowvol":[("hour_uk",">=12"),("hour_uk","<18"),("vol_ratio","<=12")],
    }
    def follows(x,spec):
        f=x["features"]
        for key,val in spec:
            v=f[key]
            if isinstance(val,str) and val.startswith("<="):
                if v is None or not v<=float(val[2:]):return False
            elif isinstance(val,str) and val.startswith(">="):
                if v is None or not v>=float(val[2:]):return False
            elif isinstance(val,str) and val.startswith("<"):
                if v is None or not v<float(val[1:]):return False
            elif v!=val:return False
        return True
    details={}
    for name,filters in focused.items():
        details[name]=[{k:v for k,v in metrics([x for x in w if follows(x,filters)]).items() if k!="days"}
                        for w in windows]
    print("FOCUSED_FILTER_WINDOWS="+json.dumps(details,separators=(",",":")),flush=True)

    print("EXIT_REASONS="+json.dumps(dict(Counter(e["reason"] for e in selected))),flush=True)
    result={"original_window_reproduced":reproducible,"expected_train_valid_holdout":expected,
            "symbols_fetched":len(data),"original_symbols_requested":len(insts),
            "baseline":baseline,"by_period":base_stats,"winner_vs_nonwinner_medians":differences,
            "categories":cats,"filters_selected_using_train_valid_only":top,
            "limitations":"Same historical selected instruments, not top200; trend not midnight reset; original 0.12% estimate only; no slippage/orderbook. Filters evaluated after selection on same trades; not resimulated if skipped signals free slots, so counterfactual portfolio PNL imperfect. No LIVE trading changes."}
    Path("pullback_winners_diagnostic_report.json").write_text(json.dumps(result,indent=2))
    print("DIAGNOSTIC_DONE",flush=True)

if __name__=="__main__":
    main()
