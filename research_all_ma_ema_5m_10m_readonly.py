"""Read-only scan of ALL common SMA/EMA periods for recorded BloFin trades.
Fixed real fills, check an MA/EMA touch within 5 or 10 mins AFTER actual exit.
Research estimates only; no live config changes or private API.
"""
from pathlib import Path
import json,time
import research_extend_5m_10m_ma_readonly as base

PERIODS=[5,7,10,20,30,50,100,200]
TYPES=["SMA","EMA"]
MINUTE=base.MINUTE
FIVE=base.FIVE

def average(fives,end_ts,period,typ):
    if typ=="SMA":count=period
    else: count=3*period
    rows=[fives.get(end_ts-i*FIVE) for i in range(count-1,-1,-1)]
    if any(r is None for r in rows):return None
    closes=[r[3] for r in rows]
    if typ=="SMA":return sum(closes)/period
    ema=sum(closes[:period])/period
    alpha=2/(period+1)
    for close in closes[period:]: ema=alpha*close+(1-alpha)*ema
    return ema

def first_touch(tr,one,fives,plusmins,period,typ):
    start=tr["closed_ms"]//MINUTE*MINUTE+MINUTE
    end=tr["closed_ms"]+plusmins*MINUTE
    cache={}
    for t in range(start,(end//MINUTE)*MINUTE+1,MINUTE):
        candle=one.get(t)
        if candle is None:return {"status":"MISSING_1M_CANDLE"}
        prev=t//FIVE*FIVE-FIVE
        if prev not in cache: cache[prev]=average(fives,prev,period,typ)
        target=cache[prev]
        if target is None:return {"status":"MISSING_AVERAGE_HISTORY"}
        if candle[2]<=target<=candle[1]:
            hyp=base.hypothetical(tr,target)
            return {"status":"TOUCHED","price":round(target,12),
                    "touch_utc":base.utc(t+MINUTE),
                    **hyp,
                    "better_than_actual":hyp["net_pct"]>tr["actual_net_pct"]}
    return {"status":"NOT_TOUCHED"}

def summarize(rows,key):
    outcomes=[x[key] for x in rows]
    touched=[x for x in outcomes if x["status"]=="TOUCHED"]
    data_missing=[x for x in outcomes if x["status"].startswith("MISSING")]
    return {"touched":len(touched),
      "not_touched":sum(x["status"]=="NOT_TOUCHED" for x in outcomes),
      "missing":len(data_missing),
      "net_wins":sum(x["net_pct"]>0 for x in touched),
      "net_losses":sum(x["net_pct"]<=0 for x in touched),
      "net_wins_with_slip":sum(x["net_pct_plus_slippage"]>0 for x in touched),
      "net_improved":sum(x["better_than_actual"] for x in touched),
      "total_net_pct_touch_only":round(sum(x["net_pct"] for x in touched),5),
      "average_net_pct_touch_only":round(sum(x["net_pct"] for x in touched)/len(touched),5) if touched else None}

def main():
    entries=json.loads(Path("research_past_34_entries.json").read_text())["entries"]
    symbols=sorted({x["inst"] for x in entries})
    series={}
    errors={}
    for i,coin in enumerate(symbols,1):
        try:
            one=base.fetch(coin,"1m",1440)
            five=base.fetch(coin,"5m",840)
            series[coin]=(one,five)
            print("SYMBOL="+json.dumps({"i":i,"of":len(symbols),"coin":coin,
                      "one":len(one),"five":len(five)}),flush=True)
        except Exception as e:
            series[coin]=({}, {})
            errors[coin]=str(e)
            print("SYMBOL_ERROR="+json.dumps({"coin":coin,"message":str(e)}),flush=True)
        time.sleep(.16)
    rows=[]
    for tr in entries:
        one,five=series[tr["inst"]]
        row={"inst":tr["inst"],"side":tr["side"],"actual_net_pct":tr["actual_net_pct"],
             "loss":tr["actual_net_pct"]<0,"closed_utc":base.utc(tr["closed_ms"])}
        for mins in (5,10):
            for typ in TYPES:
                for p in PERIODS:
                    key=f"{typ}{p}_plus{mins}m"
                    row[key]=first_touch(tr,one,five,mins,p,typ)
        rows.append(row)
    summary={}
    for mins in (5,10):
        for typ in TYPES:
            for p in PERIODS:
                key=f"{typ}{p}_plus{mins}m"
                summary[key]={"all":summarize(rows,key),
                              "original_losses":summarize([x for x in rows if x["loss"]],key),
                              "original_wins":summarize([x for x in rows if not x["loss"]],key)}
    output={"mode":"READ_ONLY_MOVING_AVERAGES_5M_TOUCH_AFTER_ACTUAL_EXIT",
            "source":"Official BloFin 1m/5m fully closed bars; 33 exact original trade entry fills",
            "ma_types":TYPES,"periods":PERIODS,"window_after_close_minutes":[5,10],
            "original_trades":len(rows),"original_losses":sum(x["loss"] for x in rows),
            "market_errors":errors,"results":summary,"individual_trades":rows,
            "limitations":"A moving-average touch is not a proven executable fill. Price can reach SL before MA. These are not full backtests of exit policy and no future MAs were used. Original fees included, optional slippage estimate 0.10%. Separate close starts after entry to next minute to avoid pre-close look-ahead. Notional shared bank and overlapping positions ignored. No live changes."}
    Path("research_all_ma_ema_5m_10m_results.json").write_text(json.dumps(output,indent=2))
    print("ALL_MA_EMA_SUMMARY="+json.dumps({k:v for k,v in output.items() if k!="individual_trades"},sort_keys=True),flush=True)
    print("ALL_MA_EMA_LOSERS="+json.dumps([x for x in rows if x["loss"]],sort_keys=True),flush=True)

if __name__=="__main__":main()
