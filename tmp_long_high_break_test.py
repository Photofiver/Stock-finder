import json, urllib.parse, urllib.request, time
from datetime import datetime, timezone

BASE = "https://openapi.blofin.com/api/v1/market/candles"
START_MS = int(datetime(2026,9,30,23,0,0,tzinfo=timezone.utc).timestamp()*1000)
END_MS = 1790914516953
M1 = 60_000
M15 = 15*M1

def get_candles(inst, after_ms):
    q = urllib.parse.urlencode({"instId": inst, "bar":"1m", "after": str(after_ms), "limit":"80"})
    req = urllib.request.Request(BASE+"?"+q, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        p = json.loads(r.read().decode())
    if str(p.get("code")) != "0":
        raise RuntimeError(p)
    out=[]
    for row in p.get("data", []):
        if len(row) >= 9 and str(row[8]) == "1":
            out.append({"ts":int(row[0]),"o":float(row[1]),"h":float(row[2]),"l":float(row[3]),"c":float(row[4])})
    return sorted(out,key=lambda x:x["ts"])

with open("blofin_live_state.json","r",encoding="utf-8") as f:
    state=json.load(f)

trades=[x for x in state.get("trade_history",[]) if x.get("side")=="LONG" and START_MS <= int(x.get("opened_ms",0)) <= END_MS]
print("LONG_TRADES",len(trades))
entered=0
kept_wins=0
kept_losses=0
filtered_wins=0
filtered_losses=0
rows=[]
for x in trades:
    inst=x["inst"]
    opened=int(x["opened_ms"])
    signal_close=(opened//M15)*M15
    target_start=signal_close-M15
    post_end=signal_close+5*M1
    candles=get_candles(inst, post_end+M1)
    sig=[b for b in candles if target_start <= b["ts"] < signal_close]
    post=[b for b in candles if signal_close <= b["ts"] < post_end]
    if len(sig)!=15 or len(post)!=5:
        rows.append((inst,opened,"DATA_MISSING",len(sig),len(post)))
        continue
    sig_high=max(b["h"] for b in sig)
    breaker=next((b for b in post if b["h"] > sig_high),None)
    broke=breaker is not None
    win=float(x.get("net_pnl_usdt",0))>0
    if broke:
        entered += 1
        kept_wins += int(win)
        kept_losses += int(not win)
    else:
        filtered_wins += int(win)
        filtered_losses += int(not win)
    rows.append({
      "inst":inst,
      "opened":datetime.fromtimestamp(opened/1000,tz=timezone.utc).isoformat(),
      "result":"WIN" if win else "LOSS",
      "signal_high":sig_high,
      "post5_high":max(b["h"] for b in post),
      "break":broke,
      "break_minute": None if not breaker else int((breaker["ts"]-signal_close)//M1)+1
    })
    time.sleep(0.15)

for row in rows:
    print(json.dumps(row,sort_keys=True))
print(json.dumps({
 "summary": {
   "original_longs":len(trades),
   "would_enter":entered,
   "would_filter_out":len(trades)-entered,
   "kept_wins":kept_wins,
   "kept_losses":kept_losses,
   "filtered_wins":filtered_wins,
   "filtered_losses":filtered_losses
 }
},sort_keys=True))
