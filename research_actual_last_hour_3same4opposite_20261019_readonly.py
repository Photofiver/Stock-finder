"""READ-ONLY BloFin 5m actual-last-hour vs alternative exit on SAME live entries.

Fixed window: 2026-10-18 23:00 UTC through 2026-10-19 00:00 UTC,
19 Oct 00:00-01:00 UK/BST. Trades were extracted from the existing
bot blofin_live_state.json record at 2026-10-19 00:01:04 UTC.
Live opened SOL/XCU at 23:01, RUNE/VVV at 23:06 (UTC).
Actual SOL/XCU/RUNE close values below are bot-recorded broker fills.
VVV was still open at 00:01:04 UTC; it is compared mark-to-market.

Fetch ONLY FOUR sets of PUBLIC market candles, with light rate limiting.
NO private API, secrets, orders, LIVE strategy modifications.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json,time,requests
START=int(datetime.fromisoformat("2026-10-18T23:00:00+00:00").timestamp()*1000)
END=int(datetime.fromisoformat("2026-10-19T00:00:00+00:00").timestamp()*1000)
FIVE=300000
SYMBOLS={
  "SOL-USDT":{"side":"SHORT","signal_ms":START,"opened_ms":1792364464743,
       "entry":103.78,"actual_close":103.81,"actual_exit_ms":1792364760697,
       "notional":6.2268,"actual_gross_usdt":-.0018},
  "XCU-USDT":{"side":"LONG","signal_ms":START,"opened_ms":1792364470064,
       "entry":6.5928,"actual_close":6.5905,"actual_exit_ms":1792364761389,
       "notional":6.5928,"actual_gross_usdt":-.0023},
  "RUNE-USDT":{"side":"SHORT","signal_ms":START+FIVE,"opened_ms":1792364767688,
       "entry":.6516,"actual_close":.6685,"actual_exit_ms":1792367159583,
       "notional":5.8644,"actual_gross_usdt":-.1521},
  "VVV-USDT":{"side":"SHORT","signal_ms":START+FIVE,"opened_ms":1792364769871,
       "entry":22.484,"actual_close":None,"actual_exit_ms":None,
       "notional":6.7452,"actual_gross_usdt":None}
}

def get_candles(inst):
    url="https://openapi.blofin.com/api/v1/market/candles"
    errors=[]
    for attempt in range(6):
        try:
            res=requests.get(url,params={"instId":inst,"bar":"5m","limit":"100"},timeout=18,
               headers={"Accept":"application/json","User-Agent":"Public-Historical-Analysis-Readonly/1"})
            if res.status_code==429:
                errors.append("HTTP 429")
                time.sleep(2*(attempt+1))
                continue
            res.raise_for_status()
            body=res.json()
            if str(body.get("code"))!="0":
                raise RuntimeError(f"BloFin market API returned {body.get('code')} {body.get('msg')}")
            bars={}
            for r in body.get("data") or []:
                if len(r)<9:continue
                ts=int(r[0])
                if ts<START-6*FIVE or ts>END:continue
                if str(r[8])!="1":continue
                bars[ts]={"open":float(r[1]),"close":float(r[4]),"volume":float(r[5])}
            return bars
        except Exception as e:
            errors.append(type(e).__name__+":"+str(e)[:100])
            time.sleep(2*(attempt+1))
    raise RuntimeError(f"Public candles unavailable {inst}: {errors}")

def color(bar):
    if bar["close"]>bar["open"]:return "GREEN"
    if bar["close"]<bar["open"]:return "RED"
    return None

def gross_result(trade,price):
    entry=trade["entry"]
    pct=100*(price/entry-1)*(1 if trade["side"]=="LONG" else -1)
    return {"gross_price_return_pct":round(pct,5),"estimated_gross_usdt":round(trade["notional"]*pct/100,6)}

def audit(inst,trade,bars):
    signal=trade["signal_ms"]
    if signal-FIVE not in bars:raise RuntimeError(f"Missing original signal bar: {inst}")
    required=range(signal,END,FIVE)
    missing=[x for x in required if x not in bars]
    if missing:raise RuntimeError(f"{inst} missing {len(missing)} 5m public candles including {missing[:3]}")
    expected="GREEN" if trade["side"]=="LONG" else "RED"
    opposite="RED" if expected=="GREEN" else "GREEN"
    source=color(bars[signal-FIVE])
    if source!=expected:raise RuntimeError(f"Entry signal candle color mismatch {inst}: {source} != {expected}")
    # First same-colour is the opening signal candle, counts toward three.
    same_streak=1
    opposite_streak=0
    visited=[]
    hypothetical_close=None
    for ts in required:
        current=bars[ts]
        c=color(current)
        if c==expected:
            same_streak+=1
            opposite_streak=0
        elif c==opposite:
            opposite_streak+=1
            same_streak=0
        else:
            same_streak=0
            opposite_streak=0
        reason=("3_IN_TRADE_DIRECTION" if same_streak>=3 else
                "4_AGAINST_TRADE_DIRECTION" if opposite_streak>=4 else None)
        bar_close=ts+FIVE
        visited.append({"closed_at_UTC":datetime.fromtimestamp(bar_close/1000,timezone.utc).strftime("%H:%M"),
                 "color":c,"vol":current["volume"],"same_streak":same_streak,
                 "opposite_streak":opposite_streak,"exit_triggered":reason})
        if reason:
            next_bar=bars.get(bar_close)
            if next_bar:
                px=next_bar["open"]
                execution="next_confirmed_5m_open"
            elif bar_close==END:
                px=current["close"]
                execution="last_completed_candle_close_proxy"
            else:
                raise RuntimeError(f"No next confirmed open for exit {inst}@{bar_close}")
            hypothetical_close={"UTC":datetime.fromtimestamp(bar_close/1000,timezone.utc).isoformat(),
                   "price":px,"reason":reason,"execution":execution,**gross_result(trade,px)}
            break
    last_completed=bars[END-FIVE]
    result={
       "inst":inst,"side":trade["side"],"notional_USDT":trade["notional"],
       "actual_broker_entry":trade["entry"],
       "actual_bot_recorded_exit":trade["actual_close"],
       "actual_bot_recorded_gross_usdt":trade["actual_gross_usdt"],
       "actual_bot_recorded_exit_time_UTC":datetime.fromtimestamp(trade["actual_exit_ms"]/1000,timezone.utc).isoformat() if trade["actual_exit_ms"] else None,
       "hypothetical_exit":hypothetical_close,
       "hypothetical_still_open_at_cutoff":hypothetical_close is None,
       "end_price_source":"public confirmed last candle close",
       "end_mark_close":last_completed["close"],
       "end_mark_gross":gross_result(trade,last_completed["close"]),
       "all_candles_used":visited
    }
    return result

def main():
    results={}
    for inst,trade in SYMBOLS.items():
        bars=get_candles(inst)
        results[inst]=audit(inst,trade,bars)
        print("COIN_RESULT="+json.dumps({k:v for k,v in results[inst].items() if k!="all_candles_used"},separators=(",",":")),flush=True)
        time.sleep(1.0)
    actual_closed=[r for r in results.values() if r["actual_bot_recorded_gross_usdt"] is not None]
    actual_closed_gross=sum(r["actual_bot_recorded_gross_usdt"] for r in actual_closed)
    realized_hyp=[]
    for r in actual_closed:
        h=r["hypothetical_exit"]
        if h is not None:
            realized_hyp.append((r["inst"],h["estimated_gross_usdt"]))
    all_alternative_to_cutoff=sum(
      (r["hypothetical_exit"] or r["end_mark_gross"])["estimated_gross_usdt"]
      for r in results.values()
    )
    actual_with_vvv_mark=actual_closed_gross+results["VVV-USDT"]["end_mark_gross"]["estimated_gross_usdt"]
    report={
     "period_UTC":"2026-10-18T23:00:00Z/2026-10-19T00:00:00Z",
     "period_UK":"2026-10-19 00:00 to 01:00 BST",
     "original_data":"actual BloFin fills preserved by LIVE bot state at 01:01 UK, public BloFin 5m candles for alternative",
     "no_commissions_slippage_funding":True,
     "simulated_exit":"3 consecutive same-colour 5m bars including entry-signal candle, or 4 consecutive opposite-color bars, first occurrence after entry; next 5m open",
     "comparison_basis":"same four actual entry fills; original position sizes; VVV is still open and marked at last closed 5m candle",
     "actual_closed_3_trades_gross_USDT":round(actual_closed_gross,6),
     "actual_3closed_plus_unrealized_VVV_to_cutoff_USDT":round(actual_with_vvv_mark,6),
     "alternative_all4_with_open_marked_at_cutoff_USDT":round(all_alternative_to_cutoff,6),
     "alternate_closed_of_3_original_closed":realized_hyp,
     "trades":results
    }
    Path("actual_last_hour_vs_3same4opposite_readonly.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    small={k:v for k,v in report.items() if k!="trades"}
    print("ACTUAL_LAST_HOUR_ALTERNATIVE_COMPARISON="+json.dumps(small,separators=(",",":")),flush=True)

if __name__=="__main__": main()
