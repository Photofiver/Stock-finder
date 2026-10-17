"""READ ONLY: no TP/SL volume-regime exit comparison on 14 days.

Frozen setup from previous tested winner:
- 5m candle direction matches user's highest-volume regime;
- previous candle opposite, current volume > previous;
- current volume >= 2.5x median of prior 20 bars;
- absolute current body >=0.25%.
Enter next candle open after closed signal.
Compare ZERO TP/SL exit by first opposing volume regime; exit by regime flip
or 60m, 240m, 24h cap; baseline uses 0.5% SL and 60m cap.
No trading credentials, LIVE parameters or exchange private endpoints.
"""
from __future__ import annotations
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timezone
from pathlib import Path
import json
import statistics
import time

import blofin_volume_meaningful_profit_8day as history
import blofin_volume_peak_trend_backtest_readonly as volume

FIVE=300_000
NOTIONAL=20.0
FEE_PCT=0.12
PERIOD_DAYS=14
WARMUP=170
STRATEGIES={
    "WITHOUT_TP_SL_60MIN":(None,12),
    "WITHOUT_TP_SL_240MIN":(None,48),
    "WITHOUT_TP_SL_24H":(None,288),
    "WITHOUT_TP_SL_ONLY_FLIP":(None,None),
    "PREVIOUS_SL_0_5_ONLY_60MIN":(0.005,12),
}


def simulation(bars, regimes, i, side, sl, max_hold, analysis_end):
    if i+1 >= len(bars):
        return None
    entry_ts=bars[i+1]["ts"]
    entry=bars[i+1]["o"]
    if entry<=0 or entry_ts>=analysis_end: return None
    worst=0.0
    best=0.0
    for j in range(i+1,len(bars)):
        bar=bars[j]
        if bar["ts"]>=analysis_end:
            # Position still open at period cutoff: mark to market at previous candle close,
            # but NOT count floating PnL as realized profit.
            prev=bars[j-1]
            pct=100*(prev["c"]/entry-1)*(1 if side=="LONG" else -1)
            return {"pct":pct,"exit_ms":bar["ts"],"reason":"OPEN_AT_PERIOD_END",
                    "mae_pct":worst,"mfe_pct":best}
        low=100*(bar["l"]/entry-1)*(1 if side=="LONG" else -1)
        high=100*(bar["h"]/entry-1)*(1 if side=="LONG" else -1)
        worst=min(worst,min(low,high))
        best=max(best,max(low,high))
        if sl is not None and worst <= -sl*100:
            return {"pct":-sl*100,"exit_ms":bar["ts"]+FIVE,"reason":"STOP",
                    "mae_pct":worst,"mfe_pct":best}
        if regimes[j][0]!=side:
            if j+1<len(bars) and bars[j+1]["ts"] <= analysis_end:
                next_open=bars[j+1]["o"]
                p=100*(next_open/entry-1)*(1 if side=="LONG" else -1)
                return {"pct":p,"exit_ms":bars[j+1]["ts"],"reason":"TREND_REVERSED",
                        "mae_pct":worst,"mfe_pct":best}
            p=100*(bar["c"]/entry-1)*(1 if side=="LONG" else -1)
            return {"pct":p,"exit_ms":bar["ts"]+FIVE,"reason":"OPEN_AT_PERIOD_END",
                    "mae_pct":worst,"mfe_pct":best}
        if max_hold is not None and (j-i)>=max_hold:
            p=100*(bar["c"]/entry-1)*(1 if side=="LONG" else -1)
            return {"pct":p,"exit_ms":bar["ts"]+FIVE,"reason":"MAX_HOLD",
                    "mae_pct":worst,"mfe_pct":best}
    last=bars[-1]
    p=100*(last["c"]/entry-1)*(1 if side=="LONG" else -1)
    return {"pct":p,"exit_ms":last["ts"]+FIVE,"reason":"OPEN_AT_PERIOD_END",
            "mae_pct":worst,"mfe_pct":best}


def candidates(inst,groups,start,end):
    by_strat=defaultdict(list)
    for bars in groups:
        regimes=volume.regime_series(bars)
        for i in range(25,len(bars)-2):
            b=bars[i]
            signal_ms=b["ts"]+FIVE
            if not (start<=signal_ms<end):
                continue
            side=regimes[i][0]
            if side is None or volume.classify_candle(b)!=side:
                continue
            prev=bars[i-1]
            if volume.classify_candle(prev)==side or b["v"]<=prev["v"] or b["o"]<=0:
                continue
            if abs(b["c"]/b["o"]-1)*100 < 0.25:
                continue
            previous_vols=[x["v"] for x in bars[i-20:i]]
            median=statistics.median(previous_vols)
            if median<=0 or b["v"]<2.5*median:
                continue
            for name,(sl,hold) in STRATEGIES.items():
                x=simulation(bars,regimes,i,side,sl,hold,end)
                if x is not None:
                    by_strat[name].append({"at":signal_ms,"inst":inst,
                                           "side":side,**x})
    return by_strat


def measure(events):
    events.sort(key=lambda e:(e["at"],e["inst"]))
    busy=0
    chosen=[]
    for e in events:
        if e["at"]<busy:
            continue
        chosen.append(e)
        busy=e["exit_ms"]
    finished=[x for x in chosen if x["reason"]!="OPEN_AT_PERIOD_END"]
    opened=[x for x in chosen if x["reason"]=="OPEN_AT_PERIOD_END"]
    pnl=sum(x["pct"]*NOTIONAL/100 for x in finished)
    fee=len(finished)*NOTIONAL*FEE_PCT/100
    floatpnl=sum(x["pct"]*NOTIONAL/100 for x in opened)
    maxmae=min((x["mae_pct"] for x in chosen),default=None)
    worstclosed=min((x["pct"] for x in finished),default=None)
    return {"signals":len(events),
            "completed_trades":len(finished),
            "open_at_end":len(opened),
            "gross_wins":sum(x["pct"]>0 for x in finished),
            "winrate_gross_pct":round(100*sum(x["pct"]>0 for x in finished)/len(finished),2) if finished else None,
            "gross_realized_usdt":round(pnl,4),
            "estimated_fees_usdt":round(fee,4),
            "net_realized_usdt":round(pnl-fee,4),
            "floating_unrealized_usdt_before_fees":round(floatpnl,4),
            "worst_trade_realized_pct":round(worstclosed,3) if worstclosed is not None else None,
            "worst_intratrade_drawdown_pct":round(maxmae,3) if maxmae is not None else None,
            "close_reasons":{k:sum(x["reason"]==k for x in chosen) for k in sorted({x["reason"] for x in chosen})},
            "selection":"One position at a time, timestamp and alphabetically sorted coin tie breaks"}


def main():
    state=json.loads(Path("blofin_live_state.json").read_text(encoding="utf-8"))
    end=(int(time.time()*1000)//FIVE)*FIVE
    oldstart=end-PERIOD_DAYS*86400000
    recentstart=end-8*86400000
    symbols=[]
    for trade in state.get("trade_history",[]):
        if not isinstance(trade,dict):continue
        try:
            rank=int(trade.get("signal_rank") or 0)
            opened=int(trade.get("opened_ms") or 0)
        except (ValueError,TypeError):continue
        inst=trade.get("inst")
        if inst and rank>=51 and rank<=100 and recentstart<=opened<end and inst not in symbols:
            symbols.append(inst)
    print(f"REQUESTED_COINS={len(symbols)}",flush=True)
    if len(symbols)<5:raise RuntimeError("Insufficient rank51-100 symbols in recent history")
    result={}
    errors={}
    t0=time.monotonic()
    with ThreadPoolExecutor(max_workers=5) as workers:
        jobs={workers.submit(history.fetch_8days,inst,end,oldstart-WARMUP*FIVE):inst for inst in symbols}
        for future in as_completed(jobs):
            inst=jobs[future]
            try:
                chunks=future.result()
                if chunks:result[inst]=chunks
                else:errors[inst]="No contiguous history"
            except Exception as e:
                errors[inst]=str(e)
    print(f"FETCHED_COINS={len(result)} MISSING={len(errors)} SECONDS={time.monotonic()-t0:.1f}",flush=True)
    if len(result)<8:raise RuntimeError("Insufficient market data")

    windows={"PRIOR_6D":(oldstart,recentstart),
             "RECENT_8D":(recentstart,end),
             "LATEST_2D":(end-2*86400000,end)}
    scores={}
    for window,(start,stop) in windows.items():
        allsignals=defaultdict(list)
        for inst,chunks in result.items():
            bystrat=candidates(inst,chunks,start,stop)
            for variant,events in bystrat.items():
                allsignals[variant].extend(events)
        scores[window]={key:measure(allsignals.get(key,[])) for key in STRATEGIES}
        print("WINDOW_COMPLETE="+window+" "+json.dumps({key:(value["completed_trades"],value["net_realized_usdt"],value["open_at_end"]) for key,value in scores[window].items()}),flush=True)
    report={"generated_utc":datetime.now(timezone.utc).isoformat(),
            "windows_utc":{key:[datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),
                                datetime.fromtimestamp(stop/1000,timezone.utc).isoformat()]
                           for key,(start,stop) in windows.items()},
            "coins_requested":len(symbols),"coins_with_candles":len(result),"missing":errors,
            "frozen_entry":"PULLBACK: trend set by largest opposing-volume bar. Green LONG, red SHORT. Enter after opposite-color prior candle, current candle color trend, volume>prior, volume>=2.5x median(prior20), candle body>=0.25%. All signals on confirmed 5m candles; entry at following open.",
            "variants":{k:{"stop_loss_pct":None if sl is None else 100*sl,"take_profit_pct":None,"max_hold_minutes":None if maxhold is None else 5*maxhold,"exit_on_opposite_volume_regime":True} for k,(sl,maxhold) in STRATEGIES.items()},
            "summary":scores,
            "assumed_roundtrip_fee_pct":FEE_PCT,
            "position_notional_usdt":NOTIONAL,
            "warning":"NO SL means unlimited downside until stop conditions, and loss > initial margin or liquidation possible on futures. Public 5m OHLCV simulation: no orderbook, slippage, actual exchange fees or liquidation. Historical coin universe chosen ex-post from 51-100 recent traded symbols, no historical rank reconstruction, one position globally arbitrary symbol tie breaks, this is diagnostic and not current LIVE execution. Open positions marked to market NOT counted realized. For 60min etc, max-hold exits may occur even if trend never flips. No LIVE settings changed."}
    Path("volume_no_sl_no_tp_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("NO_SL_TP_RESULT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    main()
