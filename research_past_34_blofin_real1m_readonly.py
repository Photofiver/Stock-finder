"""Read only: actual BloFin 1-minute candles after each actual entry, test 1H/4H holds.
No trading credentials or requests to private/order endpoints. Does not alter LIVE.
Conservative stop-first within ambiguous same 1m candle.
"""
import collections
import json
import math
import subprocess
import time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode

MINUTE=60000
COST_FEE_PCT=.12
SLIPPAGE_PCT=.10
COMBINED_COST=COST_FEE_PCT+SLIPPAGE_PCT

def asiso(ms):return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()

def candles(inst):
    url="https://openapi.blofin.com/api/v1/market/candles?"+urlencode({"instId":inst,"bar":"1m","limit":"1440"})
    output=None
    for trial in range(3):
        x=subprocess.run(["curl","--silent","--show-error","--fail","--max-time","25",
          "-H","Accept: application/json","-H","User-Agent: Mozilla/5.0 (X11; Linux x86_64) Chrome/132.0 Safari/537.36",
          url],capture_output=True,text=True,timeout=30)
        if x.returncode==0:
            output=json.loads(x.stdout)
            if str(output.get("code"))=="0":break
        if trial==2:raise RuntimeError(f"candle fetch failed: {inst}, curl={x.returncode}, msg={x.stderr[:200]}")
        time.sleep(1.5*(trial+1))
    rows={}
    for a in output.get("data") or []:
        try:
            ts=int(a[0]);o,h,l,c,v=map(float,a[1:6]);confirmed=str(a[8])
            if confirmed!="1" or not all(map(math.isfinite,(o,h,l,c,v))) or min(o,h,l,c)<=0 or l>min(o,c) or max(o,c)>h:continue
            rows[ts]=(o,h,l,c,v)
        except (IndexError,TypeError,ValueError):
            pass
    return rows

def context(tr,rows,minutes):
    opened=tr["opened_ms"]
    first_full=(opened//MINUTE+1)*MINUTE
    target=opened+minutes*MINUTE
    target_start=target//MINUTE*MINUTE
    expected=(target_start-first_full)//MINUTE+1
    present=sum(t in rows for t in range(first_full,target_start+1,MINUTE))
    bucket=rows.get(target_start)
    if not bucket:return {"status":"UNKNOWN_END_PRICE","observed_bars":present,"expected_bars":expected}
    sign=1 if tr["side"]=="LONG" else -1
    gross=sign*(bucket[3]/tr["price"]-1)*100
    return {"status":"OBSERVED","gross_pct":round(gross,5),
      "net_after_fees_pct":round(gross-COST_FEE_PCT,5),
      "net_after_fees_slippage_pct":round(gross-COMBINED_COST,5),
      "full_path":present==expected,"observed_bars":present,"expected_bars":expected,
      "exit_bucket_utc":asiso(target_start+MINUTE)}

def simulate(tr,rows,minutes,tp,sl):
    opened=tr["opened_ms"]
    start=(opened//MINUTE+1)*MINUTE
    finish=(opened+minutes*MINUTE)//MINUTE*MINUTE
    sign=1 if tr["side"]=="LONG" else -1
    entry=tr["price"]
    upper=entry*(1+sign*tp/100)
    lower=entry*(1-sign*sl/100)
    # The initial partial 1m candle is not simulated, avoiding pre-fill look-ahead.
    for ts in range(start,finish+1,MINUTE):
        b=rows.get(ts)
        if b is None:return {"status":"MISSING_PRIOR_BAR","gap_utc":asiso(ts)}
        o,h,l,c,v=b
        if sign==1:
            stop_now=l<=lower;target_now=h>=upper
            gapstop=o<=lower;gaptp=o>=upper
        else:
            stop_now=h>=lower;target_now=l<=upper
            gapstop=o>=lower;gaptp=o<=upper
        if gapstop:exitp=o;reason="GAP_SL"
        elif gaptp:exitp=upper;reason="GAP_TP"
        elif stop_now:exitp=lower;reason="SL" + ("_BOTH_HIT_STOP_FIRST" if target_now else "")
        elif target_now:exitp=upper;reason="TP"
        else:continue
        gross=sign*(exitp/entry-1)*100
        return {"status":"CLOSED","why":reason,"closed_utc":asiso(ts+MINUTE),
          "gross_pct":round(gross,5),"net_after_fees_pct":round(gross-COST_FEE_PCT,5),
          "net_after_fees_slippage_pct":round(gross-COMBINED_COST,5)}
    return {"status":"NO_EXIT_TRIGGER_BEFORE_HORIZON"}

def stats(objs,key):
    usable=[x for x in objs if x.get("status")==key]
    return {"count":len(usable),"missing_or_unresolved":len(objs)-len(usable),
      "positive_after_fees":sum(x["net_after_fees_pct"]>0 for x in usable),
      "negative_after_fees":sum(x["net_after_fees_pct"]<=0 for x in usable),
      "positive_after_fees_slippage":sum(x["net_after_fees_slippage_pct"]>0 for x in usable),
      "negative_after_fees_slippage":sum(x["net_after_fees_slippage_pct"]<=0 for x in usable),
      "sum_net_pct":round(sum(x["net_after_fees_slippage_pct"] for x in usable),4),
      "average_net_pct":round(sum(x["net_after_fees_slippage_pct"] for x in usable)/len(usable),5) if usable else None}

def main():
    data=json.loads(Path("research_past_34_entries.json").read_text())["entries"]
    unique=sorted(set(x["inst"] for x in data))
    fetched={};errors={}
    for i,inst in enumerate(unique,1):
        try:
            fetched[inst]=candles(inst)
            print("MARKET_FETCH="+json.dumps({"index":i,"of":len(unique),"inst":inst,
                "bars":len(fetched[inst]),"first":asiso(min(fetched[inst])) if fetched[inst] else None,
                "last":asiso(max(fetched[inst])) if fetched[inst] else None}),flush=True)
        except Exception as e:
            errors[inst]=str(e)
            print("MARKET_ERROR="+json.dumps({"index":i,"of":len(unique),"inst":inst,"error":str(e)}),flush=True)
        time.sleep(.27)
    results=[]
    for tr in data:
        rows=fetched.get(tr["inst"]) or {}
        obs={h:context(tr,rows,minutes) for h,minutes in [("1h",60),("4h",240)]}
        scenarios={h:simulate(tr,rows,minutes,tp,sl) for h,minutes,tp,sl in [
          ("1h_tp0_5_sl0_5",60,.5,.5),
          ("4h_tp0_5_sl0_5",240,.5,.5),
          ("4h_tp1_sl0_5",240,1,.5),
          ("4h_tp1_5_sl1",240,1.5,1),
          ("4h_tp2_sl1",240,2,1)]}
        results.append({"inst":tr["inst"],"side":tr["side"],"opened_utc":asiso(tr["opened_ms"]),
                        "actual_close_utc":asiso(tr["closed_ms"]),"actual_net_pct":tr["actual_net_pct"],
                        "actual_loser":tr["actual_net_pct"]<0,
                        "observations":obs,"scenarios":scenarios})
    los=[r for r in results if r["actual_loser"]]
    summary={"actual_loss_count":len(los),"all_trades":len(results)}
    for h in ("1h","4h"):
        obs=[r["observations"][h] for r in los]
        stat=stats(obs,"OBSERVED")
        stat["complete_1m_paths"]=sum(x.get("full_path",False) for x in obs)
        summary[h]=stat
    for h in ("1h_tp0_5_sl0_5","4h_tp0_5_sl0_5","4h_tp1_sl0_5","4h_tp1_5_sl1","4h_tp2_sl1"):
        sim=[r["scenarios"][h] for r in los]
        z=stats(sim,"CLOSED")
        z["closed_by_tp"]=sum(x.get("why","").endswith("TP") for x in sim)
        z["closed_by_sl"]=sum("SL" in x.get("why","") for x in sim)
        summary[h]=z
    report={"mode":"RETROSPECTIVE_FIXED_ORIGINAL_ENTRIES_HOLD_1H_4H",
            "source":"BloFin official publicly confirmed 1m futures candlesticks; 33 verified entry prices/times",
            "fees_pct":COST_FEE_PCT,"slippage_estimate_pct":SLIPPAGE_PCT,
            "cutoff_original_close_uk":"2026-10-23T03:04:50+01:00",
            "market_fetch_errors":errors,"markets_fetched":len(fetched),
            "summary":summary,"trade_results":results,
            "limitations":"Not a simulation of different 4H-bar entry strategy: holds original 5m fills. If a trade remained open it changes bankroll availability for later overlapping trades. No funding estimate. Execution slippage estimate only; exact live fees may vary. Same-minute TP SL stop-first. Ignore partial first entry minute; may miss early SL/TP. Endpoint 1m bar close up to a minute after exact target time. Future horizon insufficient is UNKNOWN not loss."}
    Path("research_past_34_blofin_real1m_result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print("REAL_BLOFIN_1H_4H_SUMMARY="+json.dumps({k:v for k,v in report.items() if k!="trade_results"},ensure_ascii=False,sort_keys=True),flush=True)
    print("REAL_BLOFIN_LOSERS="+json.dumps(los,ensure_ascii=False),flush=True)

if __name__=="__main__":main()
