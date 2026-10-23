"""Read-only actual 4H candles BloFin backtest. NO live orders or private API.
Using current TOP200 by 24h increase introduces survivorship/selection bias.
A 4H candle can cross TP and SL in unknown order: pessimistically SL first.
"""
import collections
import concurrent.futures
import json
import math
import statistics
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import urlopen

BASE="https://openapi.blofin.com"
FOUR=4*60*60*1000
FEE_PERCENT=0.12
SLIPPAGE_PERCENT=0.10
COST_PERCENT=FEE_PERCENT+SLIPPAGE_PERCENT
MAX_POSITION_USDT=20.0

def api(endpoint,**params):
    url=BASE+endpoint+"?"+urlencode(params)
    for attempt in range(4):
        try:
            with urlopen(url,timeout=20) as r:
                response=json.load(r)
            if str(response.get("code"))!="0":
                raise RuntimeError("BloFin API nonzero status")
            return response.get("data") or []
        except Exception:
            if attempt==3:
                raise
            time.sleep(.6*(attempt+1))

def top200():
    instruments={
        r["instId"] for r in api("/api/v1/market/instruments")
        if r.get("instType")=="SWAP" and r.get("state")=="live"
        and r.get("settleCurrency")=="USDT"
        and r.get("contractType")=="linear"
        and str(r.get("assetClass") or "").lower()=="crypto"
    }
    tickers=[]
    for row in api("/api/v1/market/tickers"):
        try:
            inst=row["instId"]
            if inst not in instruments: continue
            last=float(row.get("last") or 0)
            op=float(row.get("open24h") or 0)
            if op>0 and last>0:
                tickers.append((100*(last/op-1),inst))
        except (KeyError,TypeError,ValueError): continue
    tickers.sort(key=lambda x:(-x[0],x[1]))
    return [inst for _,inst in tickers[:200]]

def candles(inst):
    data=api("/api/v1/market/candles",instId=inst,bar="4H",limit="240")
    result={}
    for r in data:
        try:
            ts=int(r[0]);o,h,l,c,v=map(float,r[1:6])
            # Futures candles have confirmed flag in field 9.
            if len(r)<9 or str(r[8])!="1" or ts%FOUR:continue
            if min(o,h,l,c)>0 and v>=0 and l<=min(o,c) and max(o,c)<=h and all(map(math.isfinite,(o,h,l,c,v))):
                result[ts]=(o,h,l,c,v)
        except (IndexError,ValueError,TypeError,OverflowError):pass
    return sorted(result.items())

def ema(series,period):
    if len(series)<period:return None
    result=sum(series[:period])/period
    k=2/(period+1)
    for x in series[period:]:result=k*x+(1-k)*result
    return result

def rsi(series):
    n=14
    if len(series)<n+1:return None
    moves=[series[i]-series[i-1] for i in range(1,len(series))]
    gain=sum(max(x,0) for x in moves[:n])/n
    loss=sum(max(-x,0) for x in moves[:n])/n
    for d in moves[n:]:
        gain=(gain*(n-1)+max(d,0))/n
        loss=(loss*(n-1)+max(-d,0))/n
    return 100 if loss==0 and gain>0 else (50 if loss==0 else 100-100/(1+gain/loss))

def signals(inst,bars):
    out=[]
    for i in range(60,len(bars)-1):
        close_ms=bars[i][0]+FOUR
        if bars[i+1][0]!=close_ms:continue
        w=bars[i-60:i+1]
        if any(w[j+1][0]-w[j][0]!=FOUR for j in range(60)):continue
        closes=[x[1][3] for x in w]
        fast,slow=ema(closes,20),ema(closes,50)
        momentum=rsi(closes)
        mean_volume=statistics.mean(x[1][4] for x in w[-21:-1])
        o,h,l,c,v=bars[i][1]
        if mean_volume<=0 or v<=mean_volume:continue
        prev_high=max(x[1][1] for x in w[-4:-1])
        prev_low=min(x[1][2] for x in w[-4:-1])
        side=None
        if fast>slow and c>fast and c>prev_high and 52<=momentum<=70:side="LONG"
        elif fast<slow and c<fast and c<prev_low and 30<=momentum<=48:side="SHORT"
        if side:out.append((close_ms,-v/mean_volume,inst,side,bars[i+1][1][0]))
    return out

def simulate(signals,data,begin,end,tp_pct,sl_pct):
    chosen=[];free=begin
    for opened,strength,inst,side,price in signals:
        if opened<begin or opened>=end or opened<free:continue
        sign=1 if side=="LONG" else -1
        target=price*(1+sign*tp_pct/100)
        stop=price*(1-sign*sl_pct/100)
        result=None
        for k in range(3): # 12h max, no open position on missing bars
            bar=data[inst].get(opened+k*FOUR)
            if bar is None:result=None;break
            o,h,l,c,v=bar
            if side=="LONG":
                if o<=stop:result=(o,"gap_stop")
                elif o>=target:result=(target,"gap_tp")
                elif l<=stop:result=(stop,"sl")
                elif h>=target:result=(target,"tp")
            else:
                if o>=stop:result=(o,"gap_stop")
                elif o<=target:result=(target,"gap_tp")
                elif h>=stop:result=(stop,"sl")
                elif l<=target:result=(target,"tp")
            if result or k==2:
                if result is None:result=(c,"timeout")
                exited=opened+(k+1)*FOUR
                if exited<=end:
                    net=sign*(result[0]/price-1)*100-COST_PERCENT
                    chosen.append((inst,side,opened,exited,net,result[1]))
                    free=exited
                break
    net_usdt=sum(row[4]*MAX_POSITION_USDT/100 for row in chosen)
    return {"trades":len(chosen),"wins":sum(x[4]>0 for x in chosen),
            "losses":sum(x[4]<=0 for x in chosen),
            "net_usdt":round(net_usdt,6),
            "net_pct_on_20_usdt":round(net_usdt/MAX_POSITION_USDT*100,4),
            "max_loss_per_position_pct":round(min((x[4] for x in chosen),default=0),4),
            "close_reasons":dict(collections.Counter(x[5] for x in chosen))}

def main():
    symbols=top200()
    print("START_RESEARCH_4H symbols="+str(len(symbols)),flush=True)
    data={};sigs=[];errors={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures={pool.submit(candles,s):s for s in symbols}
        for future in concurrent.futures.as_completed(futures):
            name=futures[future]
            try:
                bars=future.result()
                if len(bars)<100:errors[name]="HISTORY_LT_100";continue
                data[name]=dict(bars)
                sigs+=signals(name,bars)
            except Exception as exc:errors[name]=type(exc).__name__
    if len(data)<80:raise RuntimeError("INSUFFICIENT_4H_DATA markets="+str(len(data))+" errors="+str(len(errors)))
    close=max(max(rows) for rows in data.values())+FOUR
    first=max(min(rows) for rows in data.values())
    # Split 60% earlier training / 40% later untouched holdout.
    mid=(first+(close-first)*6//10)//FOUR*FOUR
    sigs.sort(key=lambda x:(x[0],x[1],x[2]))
    cases={}
    for title,tp,sl in [("tp1_5_sl1",1.5,1),("tp2_sl1",2,1),("tp2_sl0_7",2,.7)]:
        cases[title]={"train":simulate(sigs,data,first,mid,tp,sl),
                      "holdout":simulate(sigs,data,mid,close,tp,sl)}
    report={"status":"RESEARCH_ONLY_NOT_LIVE","timeframe":"actual_confirmed_4H",
            "market_selection":"current_24h_top200_not_historical_ranks",
            "markets":len(data),"failed_markets":len(errors),"signals":len(sigs),
            "train_start":datetime.fromtimestamp(first/1000,timezone.utc).isoformat(),
            "train_end":datetime.fromtimestamp(mid/1000,timezone.utc).isoformat(),
            "holdout_end":datetime.fromtimestamp(close/1000,timezone.utc).isoformat(),
            "cost_roundtrip_pct":COST_PERCENT,"notional_usdt":MAX_POSITION_USDT,
            "results":cases,
            "caveats":"Not a LIVE backtest: present-day universe introduces selection bias, price gaps and funding/spread may worsen returns; OHLC unknown path uses SL first. 1x, one position, 12h exit. Results do not guarantee profit."}
    print("FOUR_HOUR_RESEARCH_RESULT="+json.dumps(report,sort_keys=True),flush=True)
    with open("research_real_4h_result.json","w") as f:json.dump(report,f,indent=2)

if __name__=="__main__": main()
