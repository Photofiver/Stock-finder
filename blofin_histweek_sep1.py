import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import blofin_live_hourly as bot

HOUR=3600000
FEE_RT_PCT=0.12
ACCOUNT_FRACTION=0.25
HARD_SL_PCT=10.0

START=int(datetime(2025,9,1,tzinfo=timezone.utc).timestamp()*1000)
END=int(datetime(2025,9,8,tzinfo=timezone.utc).timestamp()*1000)
FETCH_AFTER=END+HOUR

_,_,meta=bot.get_universe()
insts=sorted(meta)

def load1h(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"1H","after":str(FETCH_AFTER),"limit":"220"})
        b=bot.decorate(bot.parse_candles(raw))
        b=[x for x in b if START-30*HOUR <= x["ts"]+HOUR <= END+HOUR]
        return inst,b
    except:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=10) as ex:
    for f in as_completed([ex.submit(load1h,i) for i in insts]):
        inst,b=f.result()
        if len(b)>=30: series[inst]=b

maps={inst:{int(x["ts"]+HOUR):(k,x) for k,x in enumerate(b)} for inst,b in series.items()}
hours=list(range(START,END,HOUR))

def sig(bars,i):
    if i<1:return None
    p=bars[i-1]; c=bars[i]
    pc=bot.candle_color(p); cc=bot.candle_color(c)
    if pc=="RED" and cc=="GREEN" and c["v"]>p["v"]: return "LONG"
    if pc=="GREEN" and cc=="RED" and c["v"]>p["v"]: return "SHORT"
    return None

top10={}
for t in hours:
    prev=t-24*HOUR
    ranked=[]
    for inst,b in series.items():
        cp=maps[inst].get(t); pp=maps[inst].get(prev)
        if not cp or not pp or pp[1]["c"]<=0: continue
        ranked.append((cp[1]["c"]/pp[1]["c"]-1,inst,cp[0]))
    ranked.sort(reverse=True)
    top10[t]=[(r,inst,i) for r,(_,inst,i) in enumerate(ranked[:10],1)]

pos=None; trades=[]; account=100.0
for t in hours:
    if pos:
        inst=pos["inst"]; pair=maps.get(inst,{}).get(t)
        if pair and t>pos["entry_t"]:
            i,b=pair
            if pos["side"]=="LONG":
                sl_price=pos["entry_price"]*0.9; sl_hit=b["l"]<=sl_price
            else:
                sl_price=pos["entry_price"]*1.1; sl_hit=b["h"]>=sl_price
            if sl_hit:
                gross=-10.0; net=gross-FEE_RT_PCT; old=account
                account*=1+ACCOUNT_FRACTION*(net/100)
                trades.append({**pos,"exit_t":t,"reason":"SL10","net_position_pct":net,"account_before":old,"account_after":account})
                pos=None
            else:
                s=sig(series[inst],i)
                if s and s!=pos["side"]:
                    px=b["c"]; e=pos["entry_price"]
                    gross=((px/e)-1)*100 if pos["side"]=="LONG" else ((e/px)-1)*100
                    net=gross-FEE_RT_PCT; old=account
                    account*=1+ACCOUNT_FRACTION*(net/100)
                    trades.append({**pos,"exit_t":t,"reason":"FLIP","net_position_pct":net,"account_before":old,"account_after":account})
                    pos=None
    if pos is None:
        for rank,inst,i in top10.get(t,[]):
            s=sig(series[inst],i)
            if s:
                pos={"inst":inst,"side":s,"entry_t":t,"entry_price":series[inst][i]["c"],"rank":rank}
                break

n=len(trades); wins=sum(x["net_position_pct"]>0 for x in trades); losses=n-wins
print("SUMMARY "+json.dumps({
 "week":"2025-09-01 to 2025-09-08 UTC",
 "series":len(series),"closed_trades":n,"wins":wins,"losses":losses,
 "win_rate":round(100*wins/n,2) if n else 0,
 "account_return_pct":round(account-100,4),
 "sl10_count":sum(x["reason"]=="SL10" for x in trades),
 "max_win_position_pct":round(max([x["net_position_pct"] for x in trades] or [0]),3),
 "max_loss_position_pct":round(min([x["net_position_pct"] for x in trades] or [0]),3)
},sort_keys=True))
