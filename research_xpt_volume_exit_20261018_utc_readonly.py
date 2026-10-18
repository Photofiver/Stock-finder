"""READ ONLY XPT-USDT 5m close audit for 2026-10-18 20:40-21:00 UTC.

No private endpoints, no broker orders, no secrets. Audit the live
volume exit logic on historical candles and print exact qualifying
green/red volumes at each completed 5m boundary.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import blofin_top200_repeat_setup_research as api
import blofin_volume_flip_both_live as live

F=300_000
OPEN=datetime(2026,10,18,20,40,tzinfo=timezone.utc)
CLOSE=datetime(2026,10,18,21,0,tzinfo=timezone.utc)
start=int(OPEN.timestamp()*1000)
end=int(CLOSE.timestamp()*1000)
api.MIN_CALL_GAP=0.2

def uk(ts):
    return datetime.fromtimestamp(ts/1000, timezone.utc).strftime("%H:%M UTC")

def main():
    data,pages=api.candles("XPT-USDT",end+F,start-20*F)
    t0=start-F
    if t0 not in data:raise RuntimeError("Missing entry red bar 20:35-20:40 UTC")
    if any(t not in data for t in range(start-15*F,end,F)):
        raise RuntimeError("Incomplete XPT candles (last ten MA bars)")
    original=data[t0]
    rows=[]
    pos={"side":"SHORT","last_same_volume":original[4]}
    for close_ms in range(start+F,end+F,F):
        ts=close_ms-F
        c=data[ts]
        mean=live._previous10_mean(data,ts)
        eligible=mean is not None and mean>0 and c[4]<=mean
        previous_same=pos["last_same_volume"]
        bars=[{"ts":i,"o":d[0],"h":d[1],"l":d[2],"c":d[3],"v":d[4]}
             for i,d in data.items() if i+F<=close_ms]
        should,new_same=live.exit_transition(pos,bars,close_ms)
        pos["last_same_volume"]=new_same
        color="GREEN" if c[3]>c[0] else "RED" if c[3]<c[0] else "DOJI"
        rows.append({"bar_start_UTC":uk(ts),"bar_close_UTC":uk(close_ms),
                     "color":color,"volume_USDT":c[4],
                     "mean10_previous_USDT":round(mean,5) if mean is not None else None,
                     "eligible_below_mean10":eligible,
                     "last_qualified_red_before_USDT":previous_same,
                     "exit_short_signal":should,
                     "last_qualified_red_after_USDT":new_same})
    report={"instrument":"XPT-USDT","entry_UTC":uk(start),
            "entry_signal_red_UTC":uk(t0),
            "entry_signal_red_volume_USDT":original[4],
            "start_uk":"2026-10-18 21:40 BST",
            "last_close_uk":"2026-10-18 22:00 BST",
            "candles":rows,
            "volume_exit_trigger_candle":next((x for x in rows if x["exit_short_signal"]),None),
            "warning":"Public candles reconstruct volume signals. Not broker order history and may differ from persisted state if bot skipped a confirmed candle."}
    print("XPT_EXIT_AUDIT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
if __name__=="__main__":main()
