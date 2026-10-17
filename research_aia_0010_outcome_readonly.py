"""One-off READ ONLY outcome of historical AIA long at 2026-10-18 00:10 UK.
Read public BloFin candle history only. Do not trade or modify LIVE."""
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import json
import blofin_top200_repeat_setup_research as m
UK=ZoneInfo("Europe/London")
entry_ts=int(datetime(2026,10,17,23,10,tzinfo=timezone.utc).timestamp()*1000)
rows=m.get_data("/api/v1/market/candles",{"instId":"AIA-USDT","bar":"5m","limit":"200"})
bars={}
for x in rows:
    if len(x)<9 or str(x[8])!="1":continue
    ts=int(x[0]);bars[ts]=tuple(float(x[i]) for i in (1,2,3,4,5))
print("BARS_TOTAL="+str(len(bars)),flush=True)
if entry_ts not in bars:
    print("RESULT="+json.dumps({"error":"Entry candle unavailable","first":min(bars) if bars else None,"last":max(bars) if bars else None}),flush=True)
    raise SystemExit(0)
entry=bars[entry_ts][0]
tp=entry*1.015;sl=entry*.99
outcome=None;review=[]
for t in range(entry_ts,entry_ts+12*300000,300000):
    if t not in bars:break
    o,h,l,c,v=bars[t]
    is_sl=l<=sl
    is_tp=h>=tp
    review.append({"uk":datetime.fromtimestamp(t/1000,UK).isoformat(),"low":l,"high":h,"close":c,"tp":is_tp,"sl":is_sl})
    if is_sl or is_tp:
        outcome={"exit_uk":datetime.fromtimestamp((t+300000)/1000,UK).isoformat(),
                 "reason":"SL" if is_sl else "TP",
                 "net_pct":-1.22 if is_sl else 1.28,
                 "ambiguous_intrabar":bool(is_sl and is_tp)}
        break
if outcome is None and len(review)==12:
    end=bars[entry_ts+11*300000][3]
    outcome={"exit_uk":datetime.fromtimestamp((entry_ts+12*300000)/1000,UK).isoformat(),
             "reason":"60_MINUTES","net_pct":round((end/entry-1)*100-.22,4)}
if outcome is None:
    last=max(t for t in bars if t>=entry_ts)
    outcome={"status":"NOT_YET_CLOSED",
             "latest_completed_bar_end_uk":datetime.fromtimestamp((last+300000)/1000,UK).isoformat(),
             "elapsed_complete_bars":len(review),
             "unrealized_pct_at_last_complete_close_after_estimated_costs":round((bars[last][3]/entry-1)*100-.22,4)}
print("AIA_ENTRY_OUTCOME="+json.dumps({"instrument":"AIA-USDT","direction":"LONG",
    "entry_uk":"2026-10-18 00:10 BST","entry_price":entry,
    "tp_level":tp,"sl_level":sl,"result":outcome,
    "bars_checked":len(review),"last_3_bars":review[-3:]},ensure_ascii=False),flush=True)
