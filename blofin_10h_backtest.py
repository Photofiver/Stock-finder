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

bars_cache={}
state={"arms":{}, "last_processed_close_ms":{}}
entries=[]

def bars_for(inst):
    if inst not in bars_cache:
        bars_cache[inst]=bot.fetch_1h(inst)
    return bars_cache[inst]

def index_for_close(bars, close_ms):
    for i,b in enumerate(bars):
        if int(b["ts"] + bot.D1H_MS)==close_ms:
            return i
    return None

for close_ms, top10 in SNAPSHOTS:
    ranks={inst:i+1 for i,inst in enumerate(top10)}
    data={}
    idxs={}
    for inst in top10:
        bars=bars_for(inst)
        i=index_for_close(bars, close_ms)
        if i is None:
            continue
        data[inst]=bars
        idxs[inst]=i
        if inst not in state["arms"]:
            direction=None
            last_cross=None
            for j in range(1,i):
                cross=bot.rsi_cross(bars,j)
                if cross:
                    direction=cross
                    last_cross=bars[j]["ts"]+bot.D1H_MS
            state["arms"][inst]={"direction":direction,"used":False,"last_cross_close_ms":last_cross}

    for inst,bars in data.items():
        i=idxs[inst]
        cross=bot.rsi_cross(bars,i)
        if cross:
            state["arms"][inst]={"direction":cross,"used":False,"last_cross_close_ms":close_ms}

    candidates=[]
    details=[]
    for inst,bars in data.items():
        i=idxs[inst]
        arm=state["arms"].get(inst,{})
        side=arm.get("direction")
        vol_ok=bool(side in ("LONG","SHORT") and bot.volume_ok(bars,i,side))
        eligible=bool(side in ("LONG","SHORT") and not arm.get("used",False) and vol_ok)
        details.append({
            "rank":ranks[inst],"inst":inst,"side":side,"used":bool(arm.get("used",False)),
            "volume_ok":vol_ok,"eligible":eligible,
            "prev_color":bot.candle_color(bars[i-1]) if i>0 else None,
            "cur_color":bot.candle_color(bars[i]),
            "prev_v":bars[i-1]["v"] if i>0 else None,"cur_v":bars[i]["v"]
        })
        if eligible:
            candidates.append((ranks[inst],inst,side))

    candidates.sort()
    chosen=None
    if candidates:
        rank,inst,side=candidates[0]
        state["arms"][inst]["used"]=True
        chosen={"rank":rank,"inst":inst,"side":side}
        entries.append({"close_ms":close_ms,**chosen})

    print("SCAN "+json.dumps({"close_ms":close_ms,"chosen":chosen,"candidates":candidates,"details":details},sort_keys=True))

print("SUMMARY "+json.dumps({"scans":len(SNAPSHOTS),"entries":len(entries),"entries_list":entries},sort_keys=True))
