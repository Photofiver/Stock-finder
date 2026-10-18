"""Read-only test exact discovered 20/0 rule on BloFin TOP10 from newest saved scan.

Uses public BloFin OHLCV and saved point-in-time top10 ranks.
Fixed latest top10; also reports point-in-time top10 across 1h as contrast.
No orders, credentials, LIVE writes, or invented TP/SL.
"""
from __future__ import annotations
import collections,json,statistics
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import blofin_top200_repeat_setup_research as api

F=300000
UK=ZoneInfo("Europe/London")
FEE=0.12
def uk(ts):return datetime.fromtimestamp(ts/1000,UK).strftime("%Y-%m-%d %H:%M")
def pct(new,old):return 100*(new/old-1) if old>0 else None
def color(b):return "GREEN" if b[3]>b[0] else "RED" if b[3]<b[0] else "DOJI"
def mean10(rows,ts):
    past=[rows.get(ts-i*F) for i in range(1,11)]
    if any(x is None for x in past):return None
    return statistics.fmean(x[4] for x in past)
def closed15(rows,ts):
    block=3*F
    laststart=((ts+F)//block-1)*block
    cur=rows.get(laststart+2*F)
    prev=rows.get(laststart-F)
    return pct(cur[3],prev[3]) if cur is not None and prev is not None else None
def check(rows,btc,ts):
    b=rows.get(ts);prev=rows.get(ts-F)
    if b is None or prev is None:return "NO_PREVIOUS_BAR",None
    if color(b)!="RED" or color(prev)!="GREEN":return "NOT_RED_AFTER_GREEN",None
    if prev[4]<=0:return "PREVIOUS_ZERO_VOLUME",None
    r=b[4]/prev[4]
    if r<3 or r>=4:return "VOLUME_NOT_3_TO_4X",None
    m10=mean10(rows,ts)
    if m10 is None or m10<=0:return "MISSING_VOLUME10",None
    if b[4]>m10:return "VOLUME_ABOVE_MEAN10",None
    p150=rows.get(ts-30*F)
    if p150 is None:return "MISSING_150MIN",None
    chg150=pct(b[3],p150[3])
    if chg150<0:return "PRIOR_150MIN_NEGATIVE",None
    c15=closed15(rows,ts)
    if c15 is None:return "MISSING_CLOSED15",None
    if c15< -0.10322961:return "CLOSED15_BELOW_THRESHOLD",None
    if b[1]<=prev[1] and b[2]>=prev[2]:return "INSIDE_PREVIOUS_BAR",None
    bt=btc.get(ts);bo=btc.get(ts-3*F)
    if bt is None or bo is None:return "MISSING_BTC15",None
    b15=pct(bt[3],bo[3])
    if b15>0.062055813:return "BTC15_TOO_HIGH",None
    return "PASS",{"volume_multiple":round(r,5),"vol_over_mean10":round(b[4]/m10,5),
                   "coin_prev150_pct":round(chg150,5),
                   "completed15_pct":round(c15,5),"btc_prior15_pct":round(b15,5)}
def simulate(coins,rows,btc,start,end,ranks=None):
    matches=[];closed=[];unclosed=[];fails=collections.Counter()
    for inst in coins:
        data=rows[inst]
        position=None
        for ts in range(start-F,end,F):
            b=data.get(ts)
            if b is None:
                fails["MISSING_BAR"]+=1
                continue
            if position is not None and ts>=position["entry"]:
                mean=mean10(data,ts)
                if mean is not None and mean>0 and b[4]<=mean:
                    if color(b)=="RED":position["last_red_volume"]=b[4]
                    elif color(b)=="GREEN" and b[4]>position["last_red_volume"]:
                        nextbar=data.get(ts+F)
                        if nextbar is not None and ts+F<=end:
                            gross=pct(position["entry_price"],nextbar[0]) # SHORT return = entry/exit - 1; match prior sim differently? below exact
                            gross=100*(1-nextbar[0]/position["entry_price"])
                            closed.append({"coin":inst,"entry_uk":uk(position["entry"]),
                                "exit_uk":uk(ts+F),"gross_pct":round(gross,6),
                                "net_pct":round(gross-FEE,6),
                                "outcome":"WIN" if gross>0 else "LOSS" if gross<0 else "FLAT"})
                            position=None
            close=ts+F
            if close<=start or close>=end:continue
            if ranks is not None and inst not in ranks.get(close,set()):continue
            status,values=check(data,btc,ts)
            fails[status]+=1
            if status!="PASS":continue
            matches.append({"coin":inst,"signal_close_uk":uk(close),**values})
            if position is None:
                nextbar=data.get(ts+F)
                if nextbar is not None and nextbar[0]>0:
                    position={"entry":ts+F,"entry_price":nextbar[0],
                              "last_red_volume":b[4]}
        if position is not None:
            unclosed.append({"coin":inst,"entry_uk":uk(position["entry"]),
                "reason":"not closed before end of hour"})
    nwin=sum(x["outcome"]=="WIN" for x in closed)
    nloss=sum(x["outcome"]=="LOSS" for x in closed)
    gross=sum(x["gross_pct"] for x in closed)
    return {"signals":len(matches),"matched_signal_details":matches,
            "closed":len(closed),"wins":nwin,"losses":nloss,
            "flat":len(closed)-nwin-nloss,"still_open":unclosed,
            "success_pct":round(nwin*100/(nwin+nloss),2) if nwin+nloss else None,
            "gross_sum_percentage_points":round(gross,6),
            "estimated_net_sum_percentage_points":round(gross-FEE*len(closed),6),
            "estimated_net_usdt_20_per_trade":round((gross-FEE*len(closed))*.2,6),
            "trades":closed,"rejection_counts":dict(fails)}
def main():
    file=Path("blofin_fast_scan_events/red_sma_5m_20261018.jsonl")
    scans=[json.loads(z) for z in file.read_text().splitlines() if z.strip()]
    scans=[s for s in scans if isinstance(s.get("top200_at_scan"),list) and len(s["top200_at_scan"])>=10]
    if not scans:raise RuntimeError("No saved scanner TOP10 ranking")
    latest=max(scans,key=lambda s:s["signal_close_ms"])
    end=int(latest["signal_close_ms"])
    start=end-12*F
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    age_min=(now-end)/60000
    # Do not pretend a stale snapshot is current if workflow checkout is outdated.
    if age_min>50:raise RuntimeError(f"saved latest TOP10 is {age_min:.1f} minutes old")
    latest_top10=list(latest["top200_at_scan"][:10])
    top10_at={int(s["signal_close_ms"]):set(s["top200_at_scan"][:10])
              for s in scans if start<int(s["signal_close_ms"])<=end}
    union=sorted(set(latest_top10).union(*top10_at.values()))
    symbols=sorted(set(union)|{"BTC-USDT"})
    print("TEST_UNIVERSE="+json.dumps({"start":uk(start),"end":uk(end),
           "age_min_at_test":round(age_min,1),"latest_top10":latest_top10,
           "rolling_union_count":len(union),"ranked_scans":len(top10_at)},ensure_ascii=False),flush=True)
    api.MIN_CALL_GAP=.3
    api.MAX_PAGES=3
    dataset={};errors={}
    for coin in symbols:
        try:
            history,pages=api.candles(coin,end+F,start-50*F)
            for t in range(start-31*F,end,F):
                if t not in history:raise ValueError("Missing OHLCV for "+uk(t))
            dataset[coin]=history
        except Exception as ex:
            errors[coin]=str(ex)
    if errors:raise RuntimeError("OHLCV_PUBLIC_DATA_ERRORS="+json.dumps(errors))
    outcomes={
       "fixed_latest_top10":simulate(latest_top10,dataset,dataset["BTC-USDT"],start,end),
       "rolling_top10_at_each_close":simulate(union,dataset,dataset["BTC-USDT"],start,end,top10_at)
    }
    final={"requested":"Top 10 last hour, same chosen SHORT strategy", "start_uk":uk(start),
      "end_uk":uk(end),"latest_top10":latest_top10,
      "rolling_top10_union":union,"rolling_ranked_scans":len(top10_at),
      "source":"GitHub scan log saved realtime TOP10 + public historical BloFin closed 5m OHLCV",
      "fees_pct_roundtrip":FEE,
      "results":outcomes,
      "limitations":"Exact thresholds fitted to separate 37 vs 26 sample. Historical simulation, no orders. No spread, slippage, funding. Entries and exits at next bar open, no TP/SL. Open positions at hour end excluded. Multiple coin trades can overlap, so percentage-point sums are not single bankroll daily returns. Fixed top10 uses ranking as at end of hour and can introduce selection hindsight; rolling version is causal point-in-time."}
    print("LAST_HOUR_FINAL="+json.dumps(final,ensure_ascii=False,separators=(",",":")),flush=True)
    Path("last_hour_top10_short20_volume_research.json").write_text(json.dumps(final,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
if __name__=="__main__":main()
