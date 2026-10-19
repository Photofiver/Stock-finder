"""READ ONLY 2h comparison of actual LIVE BloFin bot fills vs 3-same/4-opposite.
Frozen bot state 2026-10-19 00:06:02 UTC (UK 01:06), window UTC
2026-10-18 22:05:00 through 2026-10-19 00:05:00 (UK 23:05-01:05).
All values are copied from LIVE exchange-fill trade_history and position state;
only PUBLIC candle endpoints, no credentials/no LIVE state changes or trades.
Both recorded fills and alternative simulated closes priced WITHOUT FEES.

Count signal candle as first same-colour bar. For every later completed 5m
candle keep consecutive direction and opposing streak; exit at earliest of
3 in trade direction or 4 opposed. Fill at NEXT 5m candle open. Positions
not exiting by cutoff are priced at last CLOSED 5m candle's close and
explicitly marked unrealized.

Two trades entered BEFORE 2h window are reported SEPARATELY from new entries;
we evaluate them from ORIGINAL entry to ensure counterfactual correctness.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
import json, requests, time
F=300000
WINDOW_START=1792361100000 # 2026-10-18T22:05:00Z
WINDOW_END=1792368300000   # 2026-10-19T00:05:00Z
# Dates/time for checkpoint verification; guard against typos.
assert datetime.fromtimestamp(WINDOW_START/1000,timezone.utc).isoformat().startswith("2026-10-18T22:05")
assert datetime.fromtimestamp(WINDOW_END/1000,timezone.utc).isoformat().startswith("2026-10-19T00:05")
TRADES=[
  # Entered BEFORE the last 2-hour window but actually closed during it
  dict(inst="BTC-USDT",side="LONG",signal=1792360500000,entry_time=1792360570510,entry=81173.4,actual_exit=81490.3,actual_exit_ms=1792361461340,notional=8.11734,actual_gross=.03169,earlier=True),
  dict(inst="XAU-USDT",side="LONG",signal=1792359900000,entry_time=1792359965233,entry=4155.6,actual_exit=4183.55,actual_exit_ms=1792363261208,notional=4.1556,actual_gross=.02795,earlier=True),
  # All new entries in the exact 2h window, from LIVE trade_history
  dict(inst="CRV-USDT",side="SHORT",signal=1792361400000,entry_time=1792361467461,entry=.342,actual_exit=.3424,actual_exit_ms=1792361763463,notional=8.55,actual_gross=-.01,earlier=False),
  dict(inst="TRADOOR-USDT",side="LONG",signal=1792361700000,entry_time=1792361766569,entry=.5867,actual_exit=.5837,actual_exit_ms=1792362655332,notional=8.2138,actual_gross=-.042,earlier=False),
  dict(inst="CRV-USDT",side="LONG",signal=1792362600000,entry_time=1792362662181,entry=.3444,actual_exit=.3464,actual_exit_ms=1792364458057,notional=8.61,actual_gross=.05,earlier=False),
  dict(inst="SOL-USDT",side="SHORT",signal=1792364400000,entry_time=1792364464743,entry=103.78,actual_exit=103.81,actual_exit_ms=1792364760697,notional=6.2268,actual_gross=-.0018,earlier=False),
  dict(inst="XCU-USDT",side="LONG",signal=1792364400000,entry_time=1792364470064,entry=6.5928,actual_exit=6.5905,actual_exit_ms=1792364761389,notional=6.5928,actual_gross=-.0023,earlier=False),
  dict(inst="RUNE-USDT",side="SHORT",signal=1792364700000,entry_time=1792364767688,entry=.6516,actual_exit=.6685,actual_exit_ms=1792367159583,notional=5.8644,actual_gross=-.1521,earlier=False),
  dict(inst="VVV-USDT",side="SHORT",signal=1792364700000,entry_time=1792364769871,entry=22.484,actual_exit=None,actual_exit_ms=None,notional=6.7452,actual_gross=None,earlier=False)
]
def utc(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()

def fetch_candles(inst,earliest):
    """Fetch only PUBLIC confirmed 5m data with pacing, report insufficiencies."""
    session=requests.Session()
    endpoint="https://openapi.blofin.com/api/v1/market/candles"
    last_error=None
    # Two pages cover the fixed last 2h comfortably even with 30m extra lookback.
    for attempt in range(5):
        try:
            res=session.get(endpoint,params={"instId":inst,"bar":"5m","limit":"180"},
              headers={"User-Agent":"VolumeExitLastTwoHoursResearch/1","Accept":"application/json"},timeout=20)
            if res.status_code==429:
                last_error="BloFin public market HTTP 429"
                time.sleep(2*(attempt+1))
                continue
            res.raise_for_status()
            obj=res.json()
            if str(obj.get("code"))!="0":
                raise RuntimeError("BloFin error "+str(obj.get("code")))
            bars={}
            for row in obj.get("data",[]):
                t=int(row[0])
                # Need signal entry candle plus whole 2h
                if t>=earliest-F and t<=WINDOW_END and len(row)>=9 and str(row[8])=="1":
                    bars[t]=(float(row[1]),float(row[4]),float(row[5]))
            return bars
        except Exception as e:
            last_error=repr(e)
            time.sleep(1.5*(attempt+1))
    raise RuntimeError(f"{inst}: public candles unavailable: {last_error}")

def gross(trade,price):
    pct=(price/trade["entry"]-1)*(1 if trade["side"]=="LONG" else -1)*100
    usdt=trade["notional"]*pct/100
    return {"gross_pct":round(pct,6),"gross_usdt":round(usdt,7)}

def alternate_exit(trade,bars):
    target="GREEN" if trade["side"]=="LONG" else "RED"
    signal_bar=bars.get(trade["signal"]-F)
    if signal_bar is None:raise RuntimeError(f"{trade['inst']} missing signal candle")
    observed="GREEN" if signal_bar[1]>signal_bar[0] else "RED" if signal_bar[1]<signal_bar[0] else "DOJI"
    if observed!=target:raise RuntimeError(f"{trade['inst']} signal candle {observed} differs {target}")
    same,opposed=1,0
    path=[]
    alt=None
    for t in range(trade["signal"],WINDOW_END-F,F):
        b=bars.get(t)
        if b is None:raise RuntimeError(f"{trade['inst']} missing 5m {utc(t)}")
        col="GREEN" if b[1]>b[0] else "RED" if b[1]<b[0] else "DOJI"
        if col==target:
            same+=1;opposed=0
        elif col!="DOJI":
            opposed+=1;same=0
        else:
            same=0;opposed=0
        why="THREE_IN_DIRECTION" if same>=3 else "FOUR_OPPOSITE" if opposed>=4 else None
        path.append({"utc_closed":utc(t+F),"color":col,"same_streak":same,"opposite_streak":opposed,"volume":b[2]})
        if why:
            nextbar=bars.get(t+F)
            if nextbar is None:raise RuntimeError(f"{trade['inst']} exit candle followed by missing next open {utc(t+F)}")
            alt={"utc_exit":utc(t+F),"reason":why,"price":nextbar[0],"estimated":gross(trade,nextbar[0])}
            break
    final=bars.get(WINDOW_END-F)
    if final is None:raise RuntimeError(f"{trade['inst']} missing final CLOSED candle")
    original=(gross(trade,trade["actual_exit"]) if trade["actual_exit"] is not None else gross(trade,final[1]))
    # Prefer known actual exchange result, which is based on order-history fills/fee accounting.
    if trade["actual_gross"] is not None:original["gross_usdt"]=trade["actual_gross"]
    result={
        "inst":trade["inst"],"side":trade["side"],"opened_UTC":utc(trade["entry_time"]),
        "signal_closed_UTC":utc(trade["signal"]),"entered_before_window":trade["earlier"],
        "entry_price_actual":trade["entry"],"notional_usdt":trade["notional"],
        "actual_exit_price":trade["actual_exit"],"actual_exit_UTC":utc(trade["actual_exit_ms"]) if trade["actual_exit_ms"] else None,
        "actual_pnl_or_mark":original,"actual_open_as_of_state":trade["actual_exit"] is None,
        "alternative_exit":alt,"alternative_pnl_or_mark":alt["estimated"] if alt else gross(trade,final[1]),
        "alternative_unrealized_as_of_cutoff":alt is None,
        "last_confirmed_close":final[1],"candle_path":path
    }
    return result

def summarize(arr):
    return {
        "trade_count":len(arr),
        "actual_closed":sum(not x["actual_open_as_of_state"] for x in arr),
        "actual_open_state_count":sum(x["actual_open_as_of_state"] for x in arr),
        "alternative_closed":sum(not x["alternative_unrealized_as_of_cutoff"] for x in arr),
        "alternative_open_at_cutoff":sum(x["alternative_unrealized_as_of_cutoff"] for x in arr),
        "original_gross_total_usdt":round(sum(x["actual_pnl_or_mark"]["gross_usdt"] for x in arr),6),
        "alternative_gross_total_usdt":round(sum(x["alternative_pnl_or_mark"]["gross_usdt"] for x in arr),6),
        "difference_usdt":round(sum(x["alternative_pnl_or_mark"]["gross_usdt"]-x["actual_pnl_or_mark"]["gross_usdt"] for x in arr),6),
    }
def main():
    all_bars={}
    for inst in sorted({t["inst"] for t in TRADES}):
        earliest=min(t["signal"] for t in TRADES if t["inst"]==inst)
        all_bars[inst]=fetch_candles(inst,earliest)
        print("FETCH="+json.dumps({"inst":inst,"bars":len(all_bars[inst])}),flush=True)
        time.sleep(1.2)
    out=[]
    for trade in TRADES:
        result=alternate_exit(trade,all_bars[trade["inst"]])
        out.append(result)
        print("TRADE="+json.dumps({k:v for k,v in result.items() if k!="candle_path"},separators=(",",":")),flush=True)
    main_items=[x for x in out if not x["entered_before_window"]]
    legacy=[x for x in out if x["entered_before_window"]]
    report={
        "source":"Recorded actual LIVE fills from GitHub state snapshot 2026-10-19 00:06 UTC; public BloFin 5m candles",
        "2h_window_UTC":f"{utc(WINDOW_START)} to {utc(WINDOW_END)}",
        "2h_window_UK":"18 Oct 23:05 to 19 Oct 01:05 BST",
        "no_fees_or_slippage":True,
        "fair_comparison":"same actual fills, sizes; historical alternative exits, not alternative entries; open marks are not realized profits",
        "new_entries_in_last_2h":summarize(main_items),
        "earlier_positions_closed_inside_2h":summarize(legacy),
        "all_active_or_closed_during_2h":summarize(out),
        "trade_results":out
    }
    Path("last_two_hours_live_vs_3same4opposite_20261019_readonly.json").write_text(json.dumps(report,indent=2,ensure_ascii=False))
    compact={k:v for k,v in report.items() if k!="trade_results"}
    print("LAST_TWO_HOURS_RESULT="+json.dumps(compact,separators=(",",":")),flush=True)

if __name__=="__main__":main()
