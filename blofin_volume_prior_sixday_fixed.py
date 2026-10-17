"""Frozen-parameter validation on 6 days PRECEDING the 8-day search.

Pure public OHLCV; no parameter fitting, no broker keys, no trade orders.
This historical set is not out-of-sample in a time-forward sense; it is an
earlier disjoint period never used for the latest search.
"""
import concurrent.futures
from datetime import datetime,timezone
import json
import time
from pathlib import Path

import blofin_volume_meaningful_profit_8day as downloader
import blofin_volume_hold_until_reversal_readonly as holding

FIVE=300000
SIX_DAYS=6*86400000
EIGHT_DAYS=8*86400000

def main():
    state=json.loads(Path("blofin_live_state.json").read_text())
    now=(int(time.time()*1000)//FIVE)*FIVE
    end=now-EIGHT_DAYS
    start=end-SIX_DAYS
    current_window_start=now-EIGHT_DAYS
    insts=[]
    for x in state.get("trade_history",[]):
        try:
            rank=int(x.get("signal_rank") or 0)
            stamp=int(x.get("opened_ms") or 0)
        except (TypeError,ValueError,AttributeError):continue
        inst=x.get("inst")
        if 51<=rank<=100 and current_window_start<=stamp<now and inst and inst not in insts:
            insts.append(inst)
    data={}
    errors={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        fs={pool.submit(downloader.fetch_8days,coin,end,start-130*FIVE):coin for coin in insts}
        for future in concurrent.futures.as_completed(fs):
            coin=fs[future]
            try:
                groups=future.result()
                if groups:data[coin]=groups
                else:errors[coin]="No contiguous bars"
            except Exception as exc:errors[coin]=str(exc)
    print(f"EARLIER_HISTORY={len(data)} FAILURES={len(errors)}",flush=True)
    if len(data)<10:
        raise RuntimeError("Insufficient earlier history")
    all_events=[]
    for inst,chunks in data.items():
        all_events.extend(holding.collect(inst,chunks,start,end))
    top=[
      ("PULLBACK",2.5,.25,"BOTH","ANY","ANY", (None,.005,12)),
      ("PEAK",.75,0.,"BOTH","ANY","ANY",(None,.01,12))
    ]
    out={}
    for fam,v,b,direction,ma,rsi,cfg in top:
        rule=(fam,v,b,direction,ma,rsi)
        exitidx=holding.EXITS.index(cfg)
        fam_events=sorted((e for e in all_events if e["family"]==fam),key=lambda x:(x["at"],x["inst"]))
        first=holding.get_matching(fam_events,rule,start,start+3*86400000)
        second=holding.get_matching(fam_events,rule,start+3*86400000,end)
        out[fam]={"parameters":holding.rule_details(rule,exitidx),
                  "first_3days":holding.eval_rule(first,exitidx),
                  "next_3days":holding.eval_rule(second,exitidx)}
    report={"period":{"start":datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),
                      "end":datetime.fromtimestamp(end/1000,timezone.utc).isoformat()},
            "coins_analyzed":len(data),"coins_missing":errors,"fixed_rules":out,
            "caution":"Out-of-period earlier dates but ex-post coin selection; exact historical rank51-100 not reconstructed; 20USDT fixed, 0.12pct fee, no spread or broker orderbook; no TP and static SL only; first-touch ambiguity conservative. LIVE unchanged."}
    Path("volume_prior_six_day_fixed_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print("EARLIER_FIXED_CHECK="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)

if __name__=="__main__":main()
