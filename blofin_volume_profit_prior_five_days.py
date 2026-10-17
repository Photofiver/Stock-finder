"""Independent robustness check of FIXED volume-flip winner on PRIOR five days.

No parameter optimization; rules selected from prior 48h train +24h test:
volume-regime flip after a larger opposing color volume bar, body 0.10%,
TP 1%, SL 1%, holding exactly one next 5m bar, estimated round-trip fee 0.12%.
Also reports body 0.25% stricter alternative; no LIVE trading credentials.
"""
import concurrent.futures
from datetime import datetime, timezone
import json
import time
from pathlib import Path
import blofin_volume_peak_trend_backtest_readonly as old

FIVE=old.FIVE
FEE=0.12
NOTIONAL=20
TP=0.01
SL=0.01
HOLD=1


def outcome(bars,i,side):
    if i+1>=len(bars):return None
    bar=bars[i+1]
    entry=bar["o"]
    if not entry:return None
    if side=="LONG":
        tp=bar["h"]>=entry*(1+TP)
        sl=bar["l"]<=entry*(1-SL)
        result=100*(bar["c"]/entry-1)
    else:
        tp=bar["l"]<=entry*(1-TP)
        sl=bar["h"]>=entry*(1+SL)
        result=100*(1-bar["c"]/entry)
    if sl:result=-SL*100
    elif tp:result=TP*100
    return (result,bar["ts"]+FIVE)


def report_for_signals(candidates):
    candidates=sorted(candidates,key=lambda x:(x["ms"],x["inst"]))
    selected=[]
    busy=-1
    for x in candidates:
        if x["ms"]<busy:
            continue
        selected.append(x)
        busy=x["end"]
    def metrics(xs):
        pnl=[x["pct"] for x in xs]
        n=len(xs)
        net=sum(pnl)*NOTIONAL/100-n*NOTIONAL*FEE/100
        daily={}
        for e in xs:
            day=datetime.fromtimestamp(e["ms"]/1000,timezone.utc).strftime("%Y-%m-%d")
            daily.setdefault(day,[]).append(e["pct"])
        return {"trades":n,
                "gross_wins":sum(p>0 for p in pnl),
                "win_rate_pct":round(100*sum(p>0 for p in pnl)/n,2) if n else None,
                "gross_usdt":round(sum(pnl)*NOTIONAL/100,5),
                "fee_usdt":round(n*NOTIONAL*FEE/100,5),
                "net_usdt":round(net,5),
                "daily_net_usdt":{k:round((sum(v)-len(v)*FEE)*NOTIONAL/100,5) for k,v in daily.items()}}
    return {"all_simultaneous":metrics(candidates),
            "at_most_one_position":metrics(selected)}


def main():
    state=json.loads(Path("blofin_live_state.json").read_text(encoding="utf-8"))
    current_end=(int(time.time()*1000)//FIVE)*FIVE
    prior_end=current_end-72*60*60_000
    prior_start=prior_end-5*24*60*60_000
    coins=[]
    for t in state.get("trade_history",[]):
        try:
            opened=int(t.get("opened_ms") or 0)
            rank=int(t.get("signal_rank") or 0)
        except (ValueError,TypeError,AttributeError):
            continue
        if prior_end<=opened<current_end and 51<=rank<=100 and t.get("inst"):
            if t["inst"] not in coins:coins.append(t["inst"])
    if not coins:
        raise RuntimeError("No coins")

    data={}
    errors={}
    start=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futuremap={pool.submit(old.fetch_candles,coin,prior_end,prior_start-120*FIVE):coin for coin in coins}
        for f in concurrent.futures.as_completed(futuremap):
            coin=futuremap[f]
            try:
                chunks,_=f.result()
                if chunks:data[coin]=chunks
                else:errors[coin]="missing contiguous candles"
            except Exception as e:
                errors[coin]=str(e)
    print(f"PRIOR_FETCHED={len(data)} ERRORS={len(errors)} SEC={time.monotonic()-start:.1f}",flush=True)

    results={}
    for minbody in (0.10,0.25):
        signals=[]
        for coin,chunks in data.items():
            for bars in chunks:
                trends=old.regime_series(bars)
                for i in range(35,len(bars)-1):
                    bar=bars[i]
                    closed=bar["ts"]+FIVE
                    if not(prior_start<=closed<prior_end):
                        continue
                    direction,flip,peak=trends[i]
                    if not flip or direction is None or bar["o"]<=0:
                        continue
                    body=100*abs(bar["c"]/bar["o"]-1)
                    if body<minbody:continue
                    sim=outcome(bars,i,direction)
                    if sim is None:continue
                    pct,end=sim
                    signals.append({"ms":closed,"inst":coin,"pct":pct,"end":end,"side":direction})
        results[f"MIN_BODY_{minbody:.2f}_PCT"]=report_for_signals(signals)
    payload={"period_utc":{"start":datetime.fromtimestamp(prior_start/1000,timezone.utc).isoformat(),
                             "end":datetime.fromtimestamp(prior_end/1000,timezone.utc).isoformat()},
             "symbols_from_recent_trades":len(coins),"coin_histories":len(data),"missing":errors,
             "fixed_parameters":{"trend_switch":"opposite colored 5m candle volume strictly exceeds peak of current trend",
                                 "entry":"next candle open", "tp_pct":1,"sl_pct":1,
                                 "hold_minutes":5,"notional_usdt":NOTIONAL,"estimated_roundtrip_fee_pct":FEE},
             "results":results,
             "limitations":"Hindsight symbol selection from recent 72h; prior five days have no historical exact rank51-100 selection. Public OHLCV approximates TP/SL; same-candle TP+SL counts SL; no spread/slippage/live MA trailing. This is PRE-training period corroboration, not a prospective future trial."}
    Path("volume_profit_prior_five_days_report.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("PRIOR_5DAY_RESULT="+json.dumps(payload,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    main()
