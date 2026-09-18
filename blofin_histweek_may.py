import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import blofin_live_hourly as bot

HOUR=3600000
FEE_RT_PCT=0.12
ACCOUNT_FRACTION=0.25
HARD_SL_PCT=10.0

START=int(datetime(2026,5,11,tzinfo=timezone.utc).timestamp()*1000)
END=int(datetime(2026,5,18,tzinfo=timezone.utc).timestamp()*1000)
FETCH_AFTER=END+HOUR

_,_,meta=bot.get_universe()
insts=sorted(meta)
print("UNIVERSE",len(insts))

def load1h(inst):
    try:
        raw=bot.market_get("/api/v1/market/candles",{
            "instId":inst,"bar":"1H","after":str(FETCH_AFTER),"limit":"220"
        })
        b=bot.decorate(bot.parse_candles(raw))
        b=[x for x in b if START-30*HOUR <= x["ts"]+HOUR <= END+HOUR]
        return inst,b
    except Exception:
        return inst,[]

series={}
with ThreadPoolExecutor(max_workers=10) as ex:
    futs=[ex.submit(load1h,i) for i in insts]
    for n,f in enumerate(as_completed(futs),1):
        inst,b=f.result()
        if len(b)>=30: series[inst]=b
        if n%50==0: print("LOAD",n,len(series))

maps={inst:{int(x["ts"]+HOUR):(k,x) for k,x in enumerate(b)} for inst,b in series.items()}
hours=list(range(START,END,HOUR))
print("PERIOD",START,END,"HOURS",len(hours),"SERIES",len(series))

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
        ret=cp[1]["c"]/pp[1]["c"]-1
        ranked.append((ret,inst,cp[0]))
    ranked.sort(reverse=True)
    top10[t]=[(r,inst,i) for r,(_,inst,i) in enumerate(ranked[:10],1)]

def run(with_sl=True):
    pos=None
    trades=[]
    account=100.0
    for t in hours:
        # Existing position: hard SL first during this just-closed hour, then volume-flip exit.
        if pos:
            inst=pos["inst"]
            pair=maps.get(inst,{}).get(t)
            if pair and t>pos["entry_t"]:
                i,b=pair
                sl_hit=False
                if with_sl:
                    if pos["side"]=="LONG":
                        sl_price=pos["entry_price"]*(1-HARD_SL_PCT/100)
                        sl_hit=b["l"]<=sl_price
                    else:
                        sl_price=pos["entry_price"]*(1+HARD_SL_PCT/100)
                        sl_hit=b["h"]>=sl_price
                if sl_hit:
                    gross=-HARD_SL_PCT
                    net=gross-FEE_RT_PCT
                    old=account
                    account*=1+ACCOUNT_FRACTION*(net/100)
                    trades.append({**pos,"exit_t":t,"exit_price":sl_price,"reason":"SL10",
                                   "gross_pct":gross,"net_position_pct":net,
                                   "account_before":old,"account_after":account,
                                   "hold_h":(t-pos["entry_t"])/HOUR})
                    pos=None
                else:
                    s=sig(series[inst],i)
                    if s and s!=pos["side"]:
                        exit_price=b["c"]
                        e=pos["entry_price"]
                        gross=((exit_price/e)-1)*100 if pos["side"]=="LONG" else ((e/exit_price)-1)*100
                        net=gross-FEE_RT_PCT
                        old=account
                        account*=1+ACCOUNT_FRACTION*(net/100)
                        trades.append({**pos,"exit_t":t,"exit_price":exit_price,"reason":"OPPOSITE_VOLUME_FLIP",
                                       "gross_pct":gross,"net_position_pct":net,
                                       "account_before":old,"account_after":account,
                                       "hold_h":(t-pos["entry_t"])/HOUR})
                        pos=None

        if pos is None:
            for rank,inst,i in top10.get(t,[]):
                s=sig(series[inst],i)
                if s:
                    pos={"inst":inst,"side":s,"entry_t":t,"entry_price":series[inst][i]["c"],"rank":rank}
                    break

    open_mark=None
    if pos:
        inst=pos["inst"]
        available=[(tm,p) for tm,p in maps.get(inst,{}).items() if pos["entry_t"]<=tm<=END]
        if available:
            tm,(i,b)=max(available,key=lambda x:x[0])
            e=pos["entry_price"]; px=b["c"]
            gross=((px/e)-1)*100 if pos["side"]=="LONG" else ((e/px)-1)*100
            open_mark={**pos,"mark_t":tm,"mark_price":px,"unrealized_position_pct":gross,
                       "unrealized_account_pct":ACCOUNT_FRACTION*gross}

    wins=sum(x["net_position_pct"]>0 for x in trades)
    losses=sum(x["net_position_pct"]<0 for x in trades)
    return {
        "closed_trades":len(trades),
        "wins":wins,"losses":losses,
        "win_rate":round(100*wins/len(trades),2) if trades else 0,
        "sum_net_position_pct":round(sum(x["net_position_pct"] for x in trades),3),
        "account_start":100.0,
        "account_end":round(account,4),
        "account_return_pct":round(account-100,4),
        "avg_win_position_pct":round(sum(x["net_position_pct"] for x in trades if x["net_position_pct"]>0)/wins,3) if wins else 0,
        "avg_loss_position_pct":round(sum(x["net_position_pct"] for x in trades if x["net_position_pct"]<0)/losses,3) if losses else 0,
        "max_win_position_pct":round(max([x["net_position_pct"] for x in trades] or [0]),3),
        "max_loss_position_pct":round(min([x["net_position_pct"] for x in trades] or [0]),3),
        "sl10_count":sum(x["reason"]=="SL10" for x in trades),
        "avg_hold_h":round(sum(x["hold_h"] for x in trades)/len(trades),2) if trades else 0,
        "open_position":open_mark,
        "trades":trades
    }

current=run(True)
raw=run(False)
print("CURRENT "+json.dumps(current,sort_keys=True))
print("RAW_NO_SL "+json.dumps(raw,sort_keys=True))
print("SUMMARY "+json.dumps({
    "week":"2026-05-11 to 2026-05-18 UTC",
    "series":len(series),
    "current_account_return_pct":current["account_return_pct"],
    "current_closed_trades":current["closed_trades"],
    "current_wins":current["wins"],"current_losses":current["losses"],
    "current_sl10_count":current["sl10_count"],
    "current_max_win_position_pct":current["max_win_position_pct"],
    "current_max_loss_position_pct":current["max_loss_position_pct"],
    "raw_no_sl_account_return_pct":raw["account_return_pct"]
},sort_keys=True))
