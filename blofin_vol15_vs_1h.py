import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import blofin_live_hourly as bot

FEE_RT_PCT=0.12
ACCOUNT_FRACTION=0.25
HARD_SL_PCT=10.0
NOW=datetime(2026,9,18,13,0,0,tzinfo=timezone.utc)
START=NOW-timedelta(days=7)
END=NOW

def ms(dt): return int(dt.timestamp()*1000)

def load_bars(inst, bar, limit):
    try:
        raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":bar,"limit":str(limit)})
        b=bot.parse_candles(raw)
        return b
    except Exception:
        return []

_,_,meta=bot.get_universe()
insts=sorted(meta)
print("UNIVERSE",len(insts))

def run_tf(bar, step_ms, limit):
    series={}
    def load(inst):
        return inst, load_bars(inst,bar,limit)
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs=[ex.submit(load,i) for i in insts]
        for n,f in enumerate(as_completed(futs),1):
            inst,b=f.result()
            if len(b)>=100:
                series[inst]=b
            if n%50==0: print(bar,"LOAD",n,len(series))

    maps={}
    for inst,b in series.items():
        maps[inst]={int(x["ts"]+step_ms):(i,x) for i,x in enumerate(b)}

    start_ms=ms(START); end_ms=ms(END)
    times=list(range(start_ms,end_ms,step_ms))

    def sig(bars,i):
        if i<1:return None
        p=bars[i-1]; c=bars[i]
        pc=bot.candle_color(p); cc=bot.candle_color(c)
        if pc=="RED" and cc=="GREEN" and c["v"]>p["v"]: return "LONG"
        if pc=="GREEN" and cc=="RED" and c["v"]>p["v"]: return "SHORT"
        return None

    top10={}
    one_day=24*60*60*1000
    for t in times:
        prev=t-one_day
        ranked=[]
        for inst,b in series.items():
            cp=maps[inst].get(t); pp=maps[inst].get(prev)
            if not cp or not pp or pp[1]["c"]<=0: continue
            ranked.append((cp[1]["c"]/pp[1]["c"]-1,inst,cp[0]))
        ranked.sort(reverse=True)
        top10[t]=[(r,inst,i) for r,(_,inst,i) in enumerate(ranked[:10],1)]

    pos=None; trades=[]; account=100.0
    for t in times:
        if pos:
            inst=pos["inst"]; pair=maps.get(inst,{}).get(t)
            if pair and t>pos["entry_t"]:
                i,b=pair
                if pos["side"]=="LONG":
                    sl_price=pos["entry_price"]*(1-HARD_SL_PCT/100)
                    sl_hit=b["l"]<=sl_price
                else:
                    sl_price=pos["entry_price"]*(1+HARD_SL_PCT/100)
                    sl_hit=b["h"]>=sl_price
                if sl_hit:
                    gross=-HARD_SL_PCT; net=gross-FEE_RT_PCT
                    old=account; account*=1+ACCOUNT_FRACTION*(net/100)
                    trades.append({**pos,"exit_t":t,"reason":"SL10","gross_pct":gross,"net_position_pct":net,
                                   "account_before":old,"account_after":account,"hold_bars":(t-pos["entry_t"])/step_ms})
                    pos=None
                else:
                    s=sig(series[inst],i)
                    if s and s!=pos["side"]:
                        px=b["c"]; e=pos["entry_price"]
                        gross=((px/e)-1)*100 if pos["side"]=="LONG" else ((e/px)-1)*100
                        net=gross-FEE_RT_PCT
                        old=account; account*=1+ACCOUNT_FRACTION*(net/100)
                        trades.append({**pos,"exit_t":t,"reason":"FLIP","gross_pct":gross,"net_position_pct":net,
                                       "account_before":old,"account_after":account,"hold_bars":(t-pos["entry_t"])/step_ms})
                        pos=None

        if pos is None:
            for rank,inst,i in top10.get(t,[]):
                s=sig(series[inst],i)
                if s:
                    pos={"inst":inst,"side":s,"entry_t":t,"entry_price":series[inst][i]["c"],"rank":rank}
                    break

    n=len(trades); wins=sum(x["net_position_pct"]>0 for x in trades); losses=sum(x["net_position_pct"]<0 for x in trades)
    return {
        "tf":bar,
        "series":len(series),
        "closed_trades":n,
        "wins":wins,"losses":losses,
        "win_rate":round(100*wins/n,2) if n else 0,
        "account_return_pct":round(account-100,4),
        "account_end":round(account,4),
        "avg_win_position_pct":round(sum(x["net_position_pct"] for x in trades if x["net_position_pct"]>0)/wins,3) if wins else 0,
        "avg_loss_position_pct":round(sum(x["net_position_pct"] for x in trades if x["net_position_pct"]<0)/losses,3) if losses else 0,
        "max_win_position_pct":round(max([x["net_position_pct"] for x in trades] or [0]),3),
        "max_loss_position_pct":round(min([x["net_position_pct"] for x in trades] or [0]),3),
        "sl10_count":sum(x["reason"]=="SL10" for x in trades),
        "avg_hold_bars":round(sum(x["hold_bars"] for x in trades)/n,2) if n else 0,
        "trades":trades
    }

r15=run_tf("15m",15*60*1000,750)
r1h=run_tf("1H",60*60*1000,220)
print("R15 "+json.dumps(r15,sort_keys=True))
print("R1H "+json.dumps(r1h,sort_keys=True))
print("SUMMARY "+json.dumps({
    "period":"2026-09-11 13:00 UTC to 2026-09-18 13:00 UTC",
    "r15_account_return_pct":r15["account_return_pct"],
    "r1h_account_return_pct":r1h["account_return_pct"],
    "r15_trades":r15["closed_trades"],
    "r1h_trades":r1h["closed_trades"],
    "r15_win_rate":r15["win_rate"],
    "r1h_win_rate":r1h["win_rate"],
    "r15_sl10":r15["sl10_count"],
    "r1h_sl10":r1h["sl10_count"]
},sort_keys=True))
