"""Read-only counterfactual for EXACT past actual entries held to 1H or 4H.
Use strictly timestamp-verified OHLCV from original GitHub 5m JSON, not later price claims.
Checks coverage; does not assume missing bars. No exchange access, private API or LIVE changes.
"""
import collections
import gc
import json
from datetime import datetime,timezone
from pathlib import Path

FIVE=300000
HOUR=12*FIVE
FOUR=48*FIVE
COST=0.22  # 0.12% round trip fee + 0.10% adverse slip estimate
FEES_ONLY=0.12

def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()

def load_trades():
    data=json.loads(Path("research_past_34_entries.json").read_text())["entries"]
    return data

def load_archived(trades):
    # Only instruments and +/- timeframe relevant to actual verified fills.
    needed=collections.defaultdict(list)
    for tr in trades:
        start=tr["opened_ms"]//FIVE*FIVE
        needed[tr["inst"]].append((start,start+FOUR+FIVE))
    out=collections.defaultdict(dict)
    conflicting=collections.defaultdict(set)
    counts=collections.Counter()
    files=sorted(Path("blofin_live_scans").glob("live_watch_history_5m*.json"))
    if not files:raise RuntimeError("No verified JSON OHLCV archive")
    for idx,path in enumerate(files,1):
        with path.open(encoding="utf8") as f:obj=json.load(f)
        for scan in obj.get("scans") or []:
            close=int(scan.get("signal_close_ms") or 0)
            if not close:continue
            for item in scan.get("instruments") or []:
                inst=item.get("inst")
                if inst not in needed:continue
                nxt=item.get("next_candle_review") or {}
                nxtclose=int(nxt.get("next_candle_close_ms") or 0)
                if nxt.get("status")!="DONE" or nxtclose!=close+FIVE:
                    continue
                ts=nxtclose-FIVE
                if not any(start<=ts<=stop for start,stop in needed[inst]):
                    continue
                try:
                    b=(float(nxt["open"]),float(nxt["high"]),float(nxt["low"]),float(nxt["close"]),float(nxt["indicators_after_5m"]["volume"]))
                except (TypeError,ValueError,KeyError):
                    counts["invalid"]+=1;continue
                if min(b[:4])<=0 or b[4]<0 or b[2]>min(b[0],b[3]) or b[1]<max(b[0],b[3]):
                    counts["nonsensical"]+=1;continue
                previous=out[inst].get(ts)
                if previous is not None and previous!=b:
                    conflicting[inst].add(ts);continue
                out[inst][ts]=b
                counts["accepted"]+=1
        if idx%5==0 or idx==len(files):
            print("COVERAGE_PROGRESS="+json.dumps({"file":idx,"of":len(files),"accepted":counts["accepted"],"symbols":len(out)}),flush=True)
        del obj
        gc.collect()
    for inst,times in conflicting.items():
        for ts in times:out[inst].pop(ts,None)
    return out,counts,conflicting

def hold(tr,rows,minutes):
    horizon_ms=minutes*60000
    opened=tr["opened_ms"]
    target=opened+horizon_ms
    # 5m candle containing exact holding deadline (closes <= 5m later)
    target_candle=target//FIVE*FIVE
    b=rows.get(target_candle)
    entry=tr["price"];direction=1 if tr["side"]=="LONG" else -1
    observed=sum(start<=ts<=target_candle for start in [opened//FIVE*FIVE] for ts in rows)
    expected=(target_candle-(opened//FIVE*FIVE))//FIVE+1
    if b is None:
        return {"status":"MISSING_HORIZON_PRICE","covered":observed,"expected":expected}
    movement=direction*(b[3]/entry-1)*100
    # Time endpoint is not necessarily executable with taker slippage!
    return {"status":"OBSERVED_ENDPOINT","covered":observed,"expected":expected,
            "complete_path":observed==expected,"horizon_time_utc":iso(target),
            "end_candle_close_utc":iso(target_candle+FIVE),
            "gross_pct":round(movement,6),"net_pct_after_fee_and_slip":round(movement-COST,6),
            "net_pct_after_fees_only":round(movement-FEES_ONLY,6)}

def stops(tr,rows,minutes,tp,sl):
    start=tr["opened_ms"]//FIVE*FIVE+FIVE
    end=(tr["opened_ms"]+minutes*60000)//FIVE*FIVE
    sign=1 if tr["side"]=="LONG" else -1
    entry=tr["price"]
    target=entry*(1+sign*tp/100)
    stop=entry*(1-sign*sl/100)
    # Can't simulate partial entry candle; only subsequent fully observed bars.
    for at in range(start,end+1,FIVE):
        b=rows.get(at)
        if b is None:return {"status":"MISSING_BAR_BEFORE_RESULT","missing_bar_utc":iso(at)}
        o,h,l,c,v=b
        if sign==1:
            isStop=l<=stop;isTP=h>=target
            openGapStop=o<=stop;openGapTP=o>=target
        else:
            isStop=h>=stop;isTP=l<=target
            openGapStop=o>=stop;openGapTP=o<=target
        if openGapStop:exitprice=o;reason="STOP_GAP"
        elif openGapTP:exitprice=target;reason="TP_GAP"
        elif isStop:exitprice=stop;reason="SL" + ("_AMBIGUOUS_STOP_FIRST" if isTP else "")
        elif isTP:exitprice=target;reason="TP"
        else:continue
        gross=sign*(exitprice/entry-1)*100
        return {"status":"CLOSED","reason":reason,"closed_utc":iso(at+FIVE),
                "net_pct":round(gross-COST,6),"gross_pct":round(gross,6)}
    return {"status":"NO_BARRIER_HIT_BEFORE_HORIZON"} # Never claim win/loss for unresolved.

def stat(rows):
    known=[r for r in rows if r["status"]=="OBSERVED_ENDPOINT"]
    return {"known":len(known),"missing":len(rows)-len(known),
            "known_wins_after_fees_slip":sum(x["net_pct_after_fee_and_slip"]>0 for x in known),
            "known_losses_after_fees_slip":sum(x["net_pct_after_fee_and_slip"]<=0 for x in known),
            "net_pcts":round(sum(x["net_pct_after_fee_and_slip"] for x in known),5),
            "known_with_complete_path":sum(x["complete_path"] for x in known)}

def main():
    tr=load_trades()
    bars,counts,conflicts=load_archived(tr)
    result=[]
    for x in tr:
        observations={label:hold(x,bars.get(x["inst"],{}),minutes) for label,minutes in [("1h",60),("4h",240)]}
        simulated={name:stops(x,bars.get(x["inst"],{}),minutes,tp,sl)
                   for name,minutes,tp,sl in [("1h_tp0_5_sl0_5",60,.5,.5),("4h_tp1_sl0_5",240,1,.5),("4h_tp1_5_sl1",240,1.5,1)]}
        result.append({"symbol":x["inst"],"side":x["side"],"open_utc":iso(x["opened_ms"]),
                      "original_net_pct":x["actual_net_pct"],"original_loss":x["actual_net_pct"]<0,
                      "observations":observations,"scenario":simulated})
    losers=[x for x in result if x["original_loss"]]
    summ={}
    for label in ("1h","4h"):
        y=[x["observations"][label] for x in losers]
        summ[label]={"losers":stat(y),"all":stat([x["observations"][label] for x in result])}
    for name in ["1h_tp0_5_sl0_5","4h_tp1_sl0_5","4h_tp1_5_sl1"]:
        arr=[x["scenario"][name] for x in losers]
        closed=[x for x in arr if x["status"]=="CLOSED"]
        summ[name]={"closed":len(closed),"tp":sum(x["reason"].startswith("TP") for x in closed),
                    "sl":sum(not x["reason"].startswith("TP") for x in closed),
                    "net_total_pct":round(sum(x["net_pct"] for x in closed),5),
                    "unresolved":len(arr)-len(closed),
                    "missing_data":sum(x["status"]=="MISSING_BAR_BEFORE_RESULT" for x in arr)}
    report={"mode":"READ_ONLY_ACTUAL_ENTRIES_EXTENDED_HOLD_1H_4H","frozen_trades":len(result),
            "actual_net_losers":len(losers),"market_data_source":"timestamp-verified archival next_candle_review 5m bars",
            "archived_bars_in_scope":counts["accepted"],"conflicting_candles_discarded":sum(map(len,conflicts.values())),
            "summary":summ,"trade_results":result,
            "limitations":"Fixed entry, alternative hold windows; NOT true 4H entry strategy. Data gaps not inferred. 4H horizon candle-close can be up to 5m after exact time; fees 0.12%+0.10% adverse execution cost. Partial entry candle risk not modeled; simulation exits at 5m high-low with stop-first ambiguity. Live positions overlap and could exceed shared bankroll."}
    Path("research_past_34_extended_hold_result.json").write_text(json.dumps(report,indent=2))
    print("PAST_LOSERS_HOLD1H4H_SUMMARY="+json.dumps({k:v for k,v in report.items() if k!="trade_results"},sort_keys=True),flush=True)
    print("PAST_LOSERS_PER_SYMBOL="+json.dumps([r for r in result if r["original_loss"]],sort_keys=True),flush=True)

if __name__=="__main__":main()
