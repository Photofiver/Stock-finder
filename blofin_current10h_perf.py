import json
import blofin_live_hourly as bot

SNAPSHOTS = [
    (1789686000000, ["ONE-USDT","CNPY-USDT","COTI-USDT","MARSCOIN-USDT","DRIFT-USDT","NEAR-USDT","PONS-USDT","UNI-USDT","USELESS-USDT","GENIUS-USDT"]),
    (1789689600000, ["ONE-USDT","CNPY-USDT","COTI-USDT","DRIFT-USDT","MARSCOIN-USDT","NEAR-USDT","PONS-USDT","GENIUS-USDT","UNI-USDT","USELESS-USDT"]),
    (1789693200000, ["ONE-USDT","DRIFT-USDT","CNPY-USDT","COTI-USDT","GENIUS-USDT","NEAR-USDT","MARSCOIN-USDT","UNI-USDT","UAI-USDT","MMT-USDT"]),
    (1789696800000, ["ONE-USDT","DRIFT-USDT","CNPY-USDT","COTI-USDT","NEAR-USDT","ARB-USDT","UNI-USDT","GENIUS-USDT","M-USDT","MMT-USDT"]),
    (1789700400000, ["ONE-USDT","DRIFT-USDT","CNPY-USDT","COTI-USDT","NEAR-USDT","ARB-USDT","APT-USDT","PONS-USDT","UNI-USDT","CHIP-USDT"]),
    (1789704000000, ["COTI-USDT","ARB-USDT","DRIFT-USDT","NEAR-USDT","UNI-USDT","CNPY-USDT","ONE-USDT","PONS-USDT","MARSCOIN-USDT","APT-USDT"]),
    (1789707600000, ["ONE-USDT","CNPY-USDT","NEAR-USDT","DRIFT-USDT","COTI-USDT","ARB-USDT","UNI-USDT","GENIUS-USDT","APT-USDT","MET-USDT"]),
    (1789711200000, ["ONE-USDT","DRIFT-USDT","NEAR-USDT","CNPY-USDT","COTI-USDT","ARB-USDT","UNI-USDT","RAY-USDT","MET-USDT","CHIP-USDT"]),
    (1789714800000, ["ONE-USDT","DRIFT-USDT","CNPY-USDT","COTI-USDT","NEAR-USDT","ARB-USDT","UNI-USDT","MET-USDT","RAY-USDT","APT-USDT"]),
    (1789718400000, ["ONE-USDT","DRIFT-USDT","CNPY-USDT","NEAR-USDT","UNI-USDT","ARB-USDT","COTI-USDT","STRK-USDT","MET-USDT","APT-USDT"]),
]

TP=0.006
SL=0.005
HOLD=5*60*60*1000
bars1h={}
bars1m={}
arms={}
position_until=0
trades=[]

def get1h(inst):
    if inst not in bars1h:
        bars1h[inst]=bot.fetch_1h(inst)
    return bars1h[inst]

def get1m(inst):
    if inst not in bars1m:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1m","limit":"1440"})
        out=[]
        for r in raw:
            try:
                if len(r)>=9 and str(r[8])=="1":
                    out.append({"ts":int(r[0]),"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4])})
            except Exception:
                pass
        bars1m[inst]=sorted(out,key=lambda x:x["ts"])
    return bars1m[inst]

def idx_for_close(bars,close_ms):
    for i,b in enumerate(bars):
        if int(b["ts"]+bot.D1H_MS)==close_ms:
            return i
    return None

def init_arm(inst,bars,i):
    if inst in arms:
        return
    direction=None
    for j in range(1,i):
        cross=bot.rsi_cross(bars,j)
        if cross:
            direction=cross
    arms[inst]=direction

def outcome(inst,start,side):
    bs=[b for b in get1m(inst) if start <= b["ts"] <= start+HOLD]
    if not bs:
        return {"result":"NO_DATA","exit_ts":start}
    entry=bs[0]["o"]
    if side=="LONG":
        tp=entry*(1+TP); sl=entry*(1-SL)
    else:
        tp=entry*(1-TP); sl=entry*(1+SL)
    for b in bs:
        if side=="LONG":
            hit_tp=b["h"]>=tp; hit_sl=b["l"]<=sl
        else:
            hit_tp=b["l"]<=tp; hit_sl=b["h"]>=sl
        if hit_tp and hit_sl:
            return {"result":"AMBIG","entry":entry,"exit_ts":b["ts"],"pnl_pct":None}
        if hit_tp:
            return {"result":"WIN","entry":entry,"exit_ts":b["ts"],"pnl_pct":TP*100}
        if hit_sl:
            return {"result":"LOSS","entry":entry,"exit_ts":b["ts"],"pnl_pct":-SL*100}
    exit_price=bs[-1]["c"]
    pnl=((exit_price/entry)-1)*100 if side=="LONG" else ((entry/exit_price)-1)*100
    return {"result":"MAX5H","entry":entry,"exit_ts":bs[-1]["ts"]+60000,"pnl_pct":pnl}

for close_ms,top10 in SNAPSHOTS:
    candidates=[]
    for rank,inst in enumerate(top10,1):
        bars=get1h(inst)
        i=idx_for_close(bars,close_ms)
        if i is None or i<1: continue
        init_arm(inst,bars,i)
        cross=bot.rsi_cross(bars,i)
        if cross:
            arms[inst]=cross
        side=arms.get(inst)
        if side in ("LONG","SHORT") and bot.volume_ok(bars,i,side):
            candidates.append((rank,inst,side))
    candidates.sort()

    if close_ms < position_until:
        print("SCAN "+json.dumps({"close_ms":close_ms,"blocked_by_open_position":True,"candidates":candidates}))
        continue

    if not candidates:
        print("SCAN "+json.dumps({"close_ms":close_ms,"chosen":None,"candidates":[]}))
        continue

    rank,inst,side=candidates[0]
    out=outcome(inst,close_ms,side)
    trade={"close_ms":close_ms,"rank":rank,"inst":inst,"side":side,**out}
    trades.append(trade)
    position_until=out["exit_ts"]
    print("SCAN "+json.dumps({"close_ms":close_ms,"chosen":[rank,inst,side],"outcome":out,"all_candidates":candidates},sort_keys=True))

wins=sum(1 for t in trades if t["result"]=="WIN")
losses=sum(1 for t in trades if t["result"]=="LOSS")
ambig=sum(1 for t in trades if t["result"]=="AMBIG")
sum_pct=sum((t.get("pnl_pct") or 0) for t in trades if t["result"]!="AMBIG")
print("SUMMARY "+json.dumps({"trades":len(trades),"wins":wins,"losses":losses,"ambiguous":ambig,"win_rate_pct":(wins/len(trades)*100 if trades else 0),"sum_pct":sum_pct,"trades_list":trades},sort_keys=True))
