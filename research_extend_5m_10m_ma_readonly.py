"""READ-ONLY: Replay +5/+10 minute extension after *actual* BloFin exit.
Also compare next MA10/MA20 touch from confirmed 5m prices, on completed 1m bars.
No private API, no trading, no LIVE writes. Trading more than original exit is
counterfactual; does not account for overlapping available bankroll.
"""
from pathlib import Path
from urllib.parse import urlencode
from datetime import datetime, timezone
import collections
import json
import math
import subprocess
import time

MINUTE=60000
FIVE=5*MINUTE
SLIPPAGE_PCT=0.10
URL="https://openapi.blofin.com/api/v1/market/candles"

def utc(ms): return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()

def fetch(inst, interval,limit):
    url=URL+"?"+urlencode({"instId":inst,"bar":interval,"limit":str(limit)})
    for retry in range(4):
        cmd=["curl","-fsS","--max-time","25",
             "-H","Accept: application/json","-H",
             "User-Agent: Mozilla/5.0 (X11; Linux x86_64) Chrome/132.0.0.0 Safari/537.36",url]
        completed=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        if completed.returncode==0:
            try:
                obj=json.loads(completed.stdout)
                if str(obj.get("code"))=="0":
                    out={}
                    for r in obj.get("data") or []:
                        if len(r)<9 or str(r[8])!="1": continue
                        ts=int(r[0]);ohlc=tuple(map(float,r[1:6]))
                        if (ts>=0 and all(math.isfinite(v) for v in ohlc) and
                           min(ohlc[:4])>0 and ohlc[4]>=0 and
                           ohlc[2]<=min(ohlc[0],ohlc[3]) and
                           ohlc[1]>=max(ohlc[0],ohlc[3])):
                            out[ts]=ohlc
                    return out
            except (ValueError, TypeError, IndexError):pass
        if retry<3:time.sleep(1+retry)
    raise RuntimeError(f"Failed public {interval} candles for {inst}")

def fee_pct(tr):
    sign=1 if tr["side"]=="LONG" else -1
    raw=sign*(tr["actual_exit"]/tr["price"]-1)*100
    actual=tr["actual_net_pct"]
    calculated=raw-actual
    if not 0.01<calculated<1.0:raise AssertionError(f"Fee mismatch {tr['inst']}: {calculated}")
    return calculated

def hypothetical(tr,price):
    sign=1 if tr["side"]=="LONG" else -1
    gross=sign*(price/tr["price"]-1)*100
    fee=fee_pct(tr)
    return {"gross_pct":round(gross,6),
            "net_pct":round(gross-fee,6),
            "net_pct_plus_slippage":round(gross-fee-SLIPPAGE_PCT,6)}

def extra_hold(tr,one,mins):
    target=tr["closed_ms"]+mins*MINUTE
    start=(target//MINUTE)*MINUTE
    candle=one.get(start)
    if not candle:
        return {"status":"NO_CONFIRMED_1M_CANDLE","target_utc":utc(target)}
    return {"status":"OBSERVED_1M_CLOSE","target_utc":utc(target),
            "candle_closed_utc":utc(start+MINUTE),
            "exit_price":candle[3],**hypothetical(tr,candle[3])}

def first_ma_touch(tr,one,fives,mins,period):
    closed=tr["closed_ms"]
    start=((closed//MINUTE)+1)*MINUTE # no pre-close minute highs or lows
    end=closed+mins*MINUTE
    past_ma={}
    for t in range(start,(end//MINUTE)*MINUTE+1,MINUTE):
        candle=one.get(t)
        if not candle:
            return {"status":"MISSING_1M_PATH","missing_utc":utc(t)}
        # The previous 5m candle ended at t//FIVE*FIVE
        prev=(t//FIVE)*FIVE-FIVE
        key=(prev,period)
        if key not in past_ma:
            points=[fives.get(prev-i*FIVE) for i in range(period-1,-1,-1)]
            past_ma[key]=(sum(row[3] for row in points)/period if all(row is not None for row in points) else None)
        target=past_ma[key]
        if target is None:
            return {"status":"MISSING_MA_HISTORY","time_utc":utc(t)}
        if candle[2]<=target<=candle[1]:
            result=hypothetical(tr,target)
            return {"status":"TOUCHED","touch_bar_end_utc":utc(t+MINUTE),
                   "moving_average":round(target,10),**result,
                   "better_than_actual":result["net_pct"]>tr["actual_net_pct"]}
    return {"status":"NOT_TOUCHED"}

def summarize(res,key,subset):
    rows=[r[key] for r in subset]
    known=[a for a in rows if a.get("status")=="OBSERVED_1M_CLOSE"]
    return {"total":len(subset),"known":len(known),"missing":len(rows)-len(known),
            "wins_after_fees":sum(a["net_pct"]>0 for a in known),
            "losses_after_fees":sum(a["net_pct"]<=0 for a in known),
            "wins_after_costs":sum(a["net_pct_plus_slippage"]>0 for a in known),
            "losses_after_costs":sum(a["net_pct_plus_slippage"]<=0 for a in known),
            "sum_net_pct":round(sum(a["net_pct"] for a in known),5),
            "sum_net_pct_plus_slippage":round(sum(a["net_pct_plus_slippage"] for a in known),5),
            "mean_net_pct":round(sum(a["net_pct"] for a in known)/len(known),5) if known else None}

def ma_summary(subset,key):
    rows=[r[key] for r in subset]
    hit=[x for x in rows if x["status"]=="TOUCHED"]
    return {"total":len(rows),"touch":len(hit),"not_touched":sum(x["status"]=="NOT_TOUCHED" for x in rows),
            "data_missing":len(rows)-len(hit)-sum(x["status"]=="NOT_TOUCHED" for x in rows),
            "net_wins_at_ma":sum(x["net_pct"]>0 for x in hit),
            "net_losses_at_ma":sum(x["net_pct"]<=0 for x in hit),
            "net_wins_at_ma_with_slippage":sum(x["net_pct_plus_slippage"]>0 for x in hit),
            "improved_vs_actual":sum(x["better_than_actual"] for x in hit),
            "sum_net_pct_of_touched":round(sum(x["net_pct"] for x in hit),5)}

def main():
    data=json.loads(Path("research_past_34_entries.json").read_text())
    trades=data["entries"]
    names=sorted({t["inst"] for t in trades})
    public={};errors={}
    for i,inst in enumerate(names,1):
        try:
            one=fetch(inst,"1m",1440)
            five=fetch(inst,"5m",330)
            public[inst]=(one,five)
            print("FETCH="+json.dumps({"market":i,"of":len(names),"inst":inst,"one":len(one),"five":len(five)}),flush=True)
        except Exception as e:
            errors[inst]=str(e)
            public[inst]=({},{})
            print("FAIL="+json.dumps({"market":i,"inst":inst,"reason":str(e)}),flush=True)
        time.sleep(.13)
    results=[]
    for tr in trades:
        one,five=public[tr["inst"]]
        r={"inst":tr["inst"],"side":tr["side"],"closed_utc":utc(tr["closed_ms"]),
           "original_net_pct":tr["actual_net_pct"],"original_net_usdt":tr["actual_net_usdt"],
           "original_lost":tr["actual_net_pct"]<0,"estimated_actual_fee_pct":round(fee_pct(tr),6)}
        for delta in [5,10]:
            r[f"plus_{delta}m"]=extra_hold(tr,one,delta)
            for period in [10,20]:
                r[f"MA{period}_within_{delta}m"]=first_ma_touch(tr,one,five,delta,period)
        results.append(r)
    losers=[r for r in results if r["original_lost"]]
    summary={"original_trades":len(results),"original_losers":len(losers),
             "original_winners":len(results)-len(losers),
             "original_total_net_pct":round(sum(r["original_net_pct"] for r in results),5),
             "original_losers_total_net_pct":round(sum(r["original_net_pct"] for r in losers),5)}
    for delta in [5,10]:
        key=f"plus_{delta}m"
        summary[key]={"all":summarize(results,key,results),"original_losers":summarize(results,key,losers)}
        for p in [10,20]:
            key=f"MA{p}_within_{delta}m"
            summary[key]={"all":ma_summary(results,key),"original_losers":ma_summary(losers,key)}
    report={"type":"COUNTERFACTUAL_RETROSPECTIVE_READ_ONLY",
            "source":"BloFin public 1m+5m confirmed candles and frozen 33 trade fills",
            "cutoff":"22 Oct 2026 11:22 UK through 23 Oct 2026 03:04 UK",
            "cost":"Actual per-position recorded round-trip fees as fraction of notional, plus separate optional 0.10% slippage",
            "markets_fetched":len(public)-len(errors),"market_failures":errors,
            "summary":summary,"trade_results":results,
            "limitations":"Original exit protection is hypothetically canceled; no intraminute fills modeled. Exit at minute close within 60sec of target time, not guaranteed executable. MA10/MA20 target is last confirmed 5m MA, touched by 1m bar high-low after actual close; touch need not fill. Need to evaluate counterfactual overlapping capital, intrabar volatility, and new broker stops before LIVE change."}
    Path("research_extend_5m_10m_ma_results.json").write_text(json.dumps(report,indent=2))
    print("EXTEND_5M_10M_MA_SUMMARY="+json.dumps({k:v for k,v in report.items() if k!="trade_results"},sort_keys=True),flush=True)
    print("EXTEND_PER_LOSER="+json.dumps(losers),flush=True)

if __name__=="__main__":main()
