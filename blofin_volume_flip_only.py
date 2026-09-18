import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import blofin_live_hourly as bot

DAYS=7
HOUR=3600000
FEE=0.12

_,_,meta=bot.get_universe()
insts=sorted(meta)

def load1h(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","limit":"260"})
        b=bot.decorate(bot.parse_candles(raw))
        return inst,b if len(b)>=80 else []
    except:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=10) as ex:
    for f in as_completed([ex.submit(load1h,i) for i in insts]):
        inst,b=f.result()
        if b: series[inst]=b

latest=max((b[-1]["ts"]+HOUR for b in series.values()),default=0)
end=(latest//HOUR)*HOUR
start=end-DAYS*24*HOUR
hours=list(range(start,end,HOUR))
maps={inst:{int(x["ts"]+HOUR):(k,x) for k,x in enumerate(b)} for inst,b in series.items()}

def color(b):
    return bot.candle_color(b)

def strict_flip_signal(bars,i):
    # User rule interpreted literally:
    # immediate colour must flip, and new bar volume must exceed the bar it flipped from.
    if i < 1: return None
    prev=bars[i-1]; cur=bars[i]
    pc=color(prev); cc=color(cur)
    if pc=="RED" and cc=="GREEN" and cur["v"] > prev["v"]:
        return "LONG"
    if pc=="GREEN" and cc=="RED" and cur["v"] > prev["v"]:
        return "SHORT"
    return None

# Historical TOP10 reconstructed from 24h change at each hour.
top10_by_hour={}
for t in hours:
    prev=t-24*HOUR
    ranked=[]
    for inst,b in series.items():
        cp=maps[inst].get(t); pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0: continue
        ranked.append((cp[1]["c"]/pp[1]["c"]-1,inst,cp[0]))
    ranked.sort(reverse=True)
    top10_by_hour[t]=[(rank,inst,i) for rank,(_,inst,i) in enumerate(ranked[:10],1)]

trades=[]
position=None

for t in hours:
    if position is not None:
        inst=position["inst"]
        pair=maps.get(inst,{}).get(t)
        if pair:
            i,b=pair
            sig=strict_flip_signal(series[inst],i)
            if sig and sig != position["side"]:
                exit_price=b["c"]
                entry=position["entry_price"]
                gross=((exit_price/entry)-1)*100 if position["side"]=="LONG" else ((entry/exit_price)-1)*100
                trades.append({
                    "inst":inst,"side":position["side"],"entry_t":position["entry_t"],"exit_t":t,
                    "entry_price":entry,"exit_price":exit_price,"gross":gross,"net":gross-FEE,
                    "hold_h":round((t-position["entry_t"])/HOUR,2),
                    "exit_reason":"OPPOSITE_VOLUME_FLIP"
                })
                position=None

    if position is None:
        for rank,inst,i in top10_by_hour.get(t,[]):
            sig=strict_flip_signal(series[inst],i)
            if sig:
                price=series[inst][i]["c"]
                position={"inst":inst,"side":sig,"entry_t":t,"entry_price":price,"rank":rank}
                break

# Mark still-open position to current end using latest close, but do not count as closed PnL.
open_position=None
if position is not None:
    inst=position["inst"]
    last_pair=maps.get(inst,{}).get(end)
    if last_pair is None:
        # take latest known bar before end
        candidates=[(tm,p) for tm,p in maps.get(inst,{}).items() if tm<=end]
        if candidates:
            tm,last_pair=max(candidates,key=lambda x:x[0])
    if last_pair:
        i,b=last_pair
        px=b["c"]
        entry=position["entry_price"]
        unreal=((px/entry)-1)*100 if position["side"]=="LONG" else ((entry/px)-1)*100
        open_position={**position,"mark_price":px,"unrealized_pct":round(unreal,3)}

n=len(trades)
wins=sum(x["net"]>0 for x in trades)
losses=sum(x["net"]<0 for x in trades)
gross=sum(x["gross"] for x in trades)
net=sum(x["net"] for x in trades)
avg_hold=sum(x["hold_h"] for x in trades)/n if n else 0

print("TRADES "+json.dumps(trades,sort_keys=True))
print("SUMMARY "+json.dumps({
    "strategy":"strict colour flip + larger opposite volume, exit on reverse flip",
    "days":DAYS,"hours":len(hours),"closed_trades":n,
    "wins":wins,"losses":losses,
    "win_rate":round(100*wins/n,2) if n else 0,
    "gross_pct":round(gross,3),"net_pct":round(net,3),
    "avg_net_pct":round(net/n,4) if n else 0,
    "avg_hold_h":round(avg_hold,2),
    "open_position":open_position
},sort_keys=True))
