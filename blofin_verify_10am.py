import json
import blofin_live_hourly as bot

CLOSE_MS=1789722000000
TOP10=["ONE-USDT","DRIFT-USDT","CNPY-USDT","UNI-USDT","STRK-USDT","NEAR-USDT","ARB-USDT","RAY-USDT","MET-USDT","FET-USDT"]

rows=[]
for inst in TOP10:
    bars=bot.fetch_1h(inst)
    i=next((j for j,b in enumerate(bars) if int(b["ts"]+bot.D1H_MS)==CLOSE_MS),None)
    if i is None or i<1:
        rows.append({"inst":inst,"error":"candle_not_found"})
        continue
    prev,cur=bars[i-1],bars[i]
    rows.append({
        "inst":inst,
        "prev_color":bot.candle_color(prev),
        "cur_color":bot.candle_color(cur),
        "prev_v":prev["v"],
        "cur_v":cur["v"],
        "vol_long":bot.volume_ok(bars,i,"LONG"),
        "vol_short":bot.volume_ok(bars,i,"SHORT"),
        "rsi_prev":prev.get("rsi"),
        "rsi_cur":cur.get("rsi"),
    })
print("VERIFY "+json.dumps(rows,sort_keys=True))
