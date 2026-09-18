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

def vol_side(bars,i):
    cur=bot.candle_color(bars[i])
    if cur=="GREEN" and bot.volume_ok(bars,i,"LONG"):
        return "LONG"
    if cur=="RED" and bot.volume_ok(bars,i,"SHORT"):
        return "SHORT"
    return None

def outcome(inst,start,side):
    bs=[b for b in get1m(inst) if start <= b["ts"] <= start+HOLD]
    if not bs:
        return {"result":"NO_DATA","exit_ts":start}
    entry=bs[0]["o"]
    tp=entry*(1+TP) if side=="LONG" else entry*(1-TP)
    sl=entry*(1-SL) if side=="LONG" else entry*(1+SL)
    for b in bs:
        hit_tp=(b["h"]>=tp if side=="LONG" else b["l"]<=tp)
        hit_sl=(b["l"]<=sl if side=="LONG" else b["h"]>=sl)
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
        if i is None or i<1:
            continue
        side=vol_side(bars,i)
        if side:
            candidates.append((rank,inst,side))
    candidates.sort()

    if close_ms < position_until:
        print("SCAN "+json.dumps({"close_ms":close_ms,"blocked":True,"candidates":candidates}))
        continue
    if not candidates:
        print("SCAN "+json.dumps({"close_ms":close_ms,"chosen":None}))
        continue

    rank,inst,side=candidates[0]
    out=outcome(inst,close_ms,side)
    trade={"close_ms":close_ms,"rank":rank,"inst":inst,"side":side,**out}
    trades.append(trade)
    position_until=out["exit_ts"]
    print("SCAN "+json.dumps({"close_ms":close_ms,"chosen":[rank,inst,side],"outcome":out,"all_candidates":candidates},sort_keys=True))

wins=sum(t["result"]=="WIN" for t in trades)
losses=sum(t["result"]=="LOSS" for t in trades)
ambig=sum(t["result"]=="AMBIG" for t in trades)
sum_pct=sum((t.get("pnl_pct") or 0) for t in trades if t["result"]!="AMBIG")
fee_roundtrip_pct=0.12
net_definite=sum_pct - fee_roundtrip_pct*(wins+losses)
print("SUMMARY "+json.dumps({"trades":len(trades),"wins":wins,"losses":losses,"ambiguous":ambig,"definite_sum_gross_pct":sum_pct,"definite_net_after_0_12pct_fee":net_definite,"trades_list":trades},sort_keys=True))
