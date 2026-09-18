import json
import blofin_live_hourly as bot

SNAPSHOTS = {
    1789689600000: ["ONE-USDT","DRIFT-USDT","CNPY-USDT","COTI-USDT","GENIUS-USDT","NEAR-USDT","MARSCOIN-USDT","UNI-USDT","UAI-USDT","MMT-USDT"],
    1789693200000: ["ONE-USDT","DRIFT-USDT","CNPY-USDT","COTI-USDT","NEAR-USDT","ARB-USDT","UNI-USDT","GENIUS-USDT","M-USDT","MMT-USDT"],
    1789696800000: ["ONE-USDT","DRIFT-USDT","CNPY-USDT","COTI-USDT","NEAR-USDT","ARB-USDT","APT-USDT","PONS-USDT","UNI-USDT","CHIP-USDT"],
    1789700400000: ["COTI-USDT","ARB-USDT","DRIFT-USDT","NEAR-USDT","UNI-USDT","CNPY-USDT","ONE-USDT","PONS-USDT","MARSCOIN-USDT","APT-USDT"],
    1789714800000: ["ONE-USDT","DRIFT-USDT","CNPY-USDT","COTI-USDT","NEAR-USDT","ARB-USDT","UNI-USDT","MET-USDT","RAY-USDT","APT-USDT"],
}

def arm_at(bars, idx):
    direction = None
    for i in range(1, idx + 1):
        cross = bot.rsi_cross(bars, i)
        if cross:
            direction = cross
    return direction

def stoch_cross_only(bars, i):
    if i < 1:
        return None
    prev, cur = bars[i-1], bars[i]
    if None in (prev.get("k"), prev.get("d"), cur.get("k"), cur.get("d")):
        return None
    if prev["k"] <= prev["d"] and cur["k"] > cur["d"]:
        return "LONG"
    if prev["k"] >= prev["d"] and cur["k"] < cur["d"]:
        return "SHORT"
    return None

rows=[]
for close_ms, coins in SNAPSHOTS.items():
    for inst in coins:
        bars = bot.fetch_1h(inst)
        idx = next((i for i,b in enumerate(bars) if int(b["ts"] + bot.D1H_MS)==close_ms), None)
        if idx is None:
            rows.append({"close_ms":close_ms,"inst":inst,"error":"candle_not_found"})
            continue
        arm=arm_at(bars, idx)
        st=stoch_cross_only(bars, idx)
        vol_long=bot.volume_ok(bars, idx, "LONG")
        vol_short=bot.volume_ok(bars, idx, "SHORT")
        vol_ok = bot.volume_ok(bars, idx, arm) if arm in ("LONG","SHORT") else False
        stoch_ok = st == arm and arm is not None
        rows.append({
            "close_ms":close_ms,
            "inst":inst,
            "arm":arm,
            "stoch_cross":st,
            "stoch_ok":stoch_ok,
            "volume_ok":vol_ok,
            "volume_long":vol_long,
            "volume_short":vol_short,
            "prev_color":bot.candle_color(bars[idx-1]) if idx>0 else None,
            "cur_color":bot.candle_color(bars[idx]),
            "prev_v":bars[idx-1]["v"] if idx>0 else None,
            "cur_v":bars[idx]["v"],
            "full_signal":bool(stoch_ok and vol_ok),
        })

valid=[r for r in rows if "error" not in r]
agg={
    "rows":len(valid),
    "stoch_fail":sum(1 for r in valid if not r["stoch_ok"]),
    "volume_fail":sum(1 for r in valid if not r["volume_ok"]),
    "both_fail":sum(1 for r in valid if (not r["stoch_ok"] and not r["volume_ok"])),
    "stoch_only_fail":sum(1 for r in valid if (not r["stoch_ok"] and r["volume_ok"])),
    "volume_only_fail":sum(1 for r in valid if (r["stoch_ok"] and not r["volume_ok"])),
    "full_signal":sum(1 for r in valid if r["full_signal"]),
    "no_arm":sum(1 for r in valid if r["arm"] is None),
}
print("AGG "+json.dumps(agg, sort_keys=True))
for r in rows:
    print("ROW "+json.dumps(r, sort_keys=True))
