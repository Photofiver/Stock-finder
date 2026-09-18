import json
from datetime import datetime, timezone
import blofin_live_hourly as bot

TRADES=[
    (1789686000000,"DRIFT-USDT"),
    (1789689600000,"UNI-USDT"),
    (1789700400000,"COTI-USDT"),
    (1789707600000,"ONE-USDT"),
    (1789711200000,"CHIP-USDT"),
]
TP=0.015
SL=0.005
HOLD=5*60*60*1000

def candles_1m(inst):
    raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1m","limit":"1440"})
    out=[]
    for r in raw:
        try:
            if len(r)>=9 and str(r[8])=="1":
                out.append({"ts":int(r[0]),"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4])})
        except Exception:
            pass
    return sorted(out,key=lambda x:x["ts"])

results=[]
for start,inst in TRADES:
    bars=[b for b in candles_1m(inst) if start <= b["ts"] <= start+HOLD]
    if not bars:
        results.append({"inst":inst,"start":start,"error":"no 1m bars"})
        continue
    first=bars[0]
    entry=first["o"]
    tp=entry*(1+TP)
    sl=entry*(1-SL)
    exit_kind="MAX5H"
    exit_price=bars[-1]["c"]
    exit_ts=bars[-1]["ts"]+60000
    ambiguous=False
    for b in bars:
        hit_tp=b["h"]>=tp
        hit_sl=b["l"]<=sl
        if hit_tp and hit_sl:
            ambiguous=True
            exit_kind="BOTH_SAME_1M"
            exit_price=None
            exit_ts=b["ts"]
            break
        if hit_sl:
            exit_kind="SL"
            exit_price=sl
            exit_ts=b["ts"]
            break
        if hit_tp:
            exit_kind="TP"
            exit_price=tp
            exit_ts=b["ts"]
            break
    pnl_pct=None if exit_price is None else (exit_price/entry-1)*100
    results.append({
        "inst":inst,"start":start,"entry":entry,"tp":tp,"sl":sl,
        "exit_kind":exit_kind,"exit_price":exit_price,"exit_ts":exit_ts,
        "pnl_pct":pnl_pct,"ambiguous":ambiguous,
        "first_bar_ts":first["ts"],"bars":len(bars)
    })
print("RESULTS "+json.dumps(results,sort_keys=True))
print("SUMMARY "+json.dumps({
    "wins":sum(1 for r in results if r.get("pnl_pct") is not None and r["pnl_pct"]>0),
    "losses":sum(1 for r in results if r.get("pnl_pct") is not None and r["pnl_pct"]<0),
    "flat":sum(1 for r in results if r.get("pnl_pct")==0),
    "ambiguous":sum(1 for r in results if r.get("ambiguous")),
    "sum_pct":sum(r.get("pnl_pct") or 0 for r in results)
},sort_keys=True))
