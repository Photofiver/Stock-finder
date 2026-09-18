import json
import blofin_live_hourly as bot

TRADES=[
    (1789686000000,"DRIFT-USDT"),
    (1789689600000,"UNI-USDT"),
    (1789700400000,"COTI-USDT"),
    (1789707600000,"ONE-USDT"),
    (1789711200000,"CHIP-USDT"),
]
SL=0.005
HOLD=5*60*60*1000
TPS=[0.0025,0.005,0.0075,0.01,0.0125,0.015]

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

rows=[]
for start,inst in TRADES:
    bars=[b for b in candles_1m(inst) if start <= b["ts"] <= start+HOLD]
    entry=bars[0]["o"]
    sl=entry*(1-SL)
    max_high=entry
    max_before_sl=entry
    sl_ts=None
    for b in bars:
        max_high=max(max_high,b["h"])
        if sl_ts is None:
            max_before_sl=max(max_before_sl,b["h"])
            if b["l"]<=sl:
                sl_ts=b["ts"]
                break
    mfe_pct=(max_before_sl/entry-1)*100

    outcomes={}
    for tp_pct in TPS:
        tp=entry*(1+tp_pct)
        result="MAX5H"
        for b in bars:
            hit_tp=b["h"]>=tp
            hit_sl=b["l"]<=sl
            if hit_tp and hit_sl:
                result="BOTH_SAME_1M"
                break
            if hit_tp:
                result="TP"
                break
            if hit_sl:
                result="SL"
                break
        outcomes[str(tp_pct)]=result

    rows.append({"inst":inst,"entry":entry,"mfe_before_sl_pct":mfe_pct,"sl_ts":sl_ts,"outcomes":outcomes})

summary={}
for tp_pct in TPS:
    key=str(tp_pct)
    summary[key]={
        "tp_wins":sum(1 for r in rows if r["outcomes"][key]=="TP"),
        "sl_losses":sum(1 for r in rows if r["outcomes"][key]=="SL"),
        "ambiguous":sum(1 for r in rows if r["outcomes"][key]=="BOTH_SAME_1M"),
    }

print("ROWS "+json.dumps(rows,sort_keys=True))
print("SUMMARY "+json.dumps(summary,sort_keys=True))
