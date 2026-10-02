import json, time, requests, math
from datetime import datetime, timezone

BASE="https://openapi.blofin.com"
STATE="blofin_live_state.json"
START_MS=int(datetime(2026,9,30,23,0,tzinfo=timezone.utc).timestamp()*1000)
BAR5=5*60*1000
BAR15=15*60*1000

with open(STATE,"r",encoding="utf-8") as f:
    state=json.load(f)

trades=[x for x in state.get("trade_history",[]) if x.get("side")=="LONG" and int(x.get("opened_ms",0))>=START_MS]
print("LONG_COUNT",len(trades))

def get_5m(inst):
    r=requests.get(BASE+"/api/v1/market/candles",params={"instId":inst,"bar":"5m","limit":"240"},timeout=25)
    r.raise_for_status()
    p=r.json()
    if str(p.get("code"))!="0":
        raise RuntimeError(p)
    out=[]
    for row in p.get("data",[]):
        if len(row)>=9 and str(row[8])=="1":
            out.append({"ts":int(row[0]),"o":float(row[1]),"h":float(row[2]),"l":float(row[3]),"c":float(row[4]),"v":float(row[5])})
    return {x["ts"]:x for x in out}

results=[]
cache={}
for tr in trades:
    inst=tr["inst"]
    if inst not in cache:
        cache[inst]=get_5m(inst)
        time.sleep(0.08)
    bars=cache[inst]
    opened=int(tr["opened_ms"])
    sig_close=(opened//BAR15)*BAR15
    sig_start=sig_close-BAR15
    need=[sig_start,sig_start+BAR5,sig_start+2*BAR5]
    sig=[bars.get(ts) for ts in need]
    nxt=bars.get(sig_close)
    if any(x is None for x in sig) or nxt is None:
        rec={"inst":inst,"opened_ms":opened,"signal_close_ms":sig_close,"ok":None,"missing":True,
             "have_signal":[x is not None for x in sig],"have_next":nxt is not None}
    else:
        signal_high=max(x["h"] for x in sig)
        next_high=nxt["h"]
        ok=next_high>signal_high
        rec={"inst":inst,"opened_ms":opened,"signal_close_ms":sig_close,"signal_high":signal_high,
             "next5_high":next_high,"breakout":ok,"win":float(tr.get("net_pnl_usdt",0))>0,
             "net":float(tr.get("net_pnl_usdt",0))}
    results.append(rec)
    print(json.dumps(rec,separators=(",",":")))

valid=[r for r in results if r.get("breakout") is not None]
entered=[r for r in valid if r["breakout"]]
skipped=[r for r in valid if not r["breakout"]]
print("SUMMARY",json.dumps({
    "total_longs":len(trades),
    "valid":len(valid),
    "would_enter":len(entered),
    "would_skip":len(skipped),
    "entered_wins":sum(1 for r in entered if r["win"]),
    "entered_losses":sum(1 for r in entered if not r["win"]),
    "skipped_wins":sum(1 for r in skipped if r["win"]),
    "skipped_losses":sum(1 for r in skipped if not r["win"]),
},separators=(",",":")))
