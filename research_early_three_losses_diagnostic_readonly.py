"""READ-ONLY factual diagnostic: 3 EARLY stop-losses 2026-10-17 UK
(SK L LONG 22:20; DRV LONG 22:30; DASH SHORT 22:35)
and winning SKL SHORT 23:05. Uses exact 390-coin cached OHLCV and
identical original model entry/pricing. No API requests, no LIVE changes.
"""
from __future__ import annotations
import gzip,json,statistics,math
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import blofin_top200_repeat_setup_research as m
FIVE=m.FIVE; UK=ZoneInfo("Europe/London")
def stamp(h,minute):
    # Date Oct 17, BST = UTC+1
    return int(datetime(2026,10,17,h-1,minute,tzinfo=timezone.utc).timestamp()*1000)
def uk(ts):
    return datetime.fromtimestamp(ts/1000,UK).strftime("%Y-%m-%d %H:%M")
wanted=[("SKL-USDT","LONG",stamp(22,20)),("DRV-USDT","LONG",stamp(22,30)),
        ("DASH-USDT","SHORT",stamp(22,35)),("SKL-USDT","SHORT",stamp(23,5))]
exacts={}
for shard in range(4):
    with gzip.open(Path("shards")/f"early_hour_{shard}.json.gz","rt",encoding="utf-8") as f:part=json.load(f)
    assert part["shard"]==shard
    for inst,side,ts in wanted:
        if inst in part["rows"]:exacts[inst]={int(t):tuple(x) for t,x in part["rows"][inst].items()}
assert len(exacts)==3
state={name:m.volume_states(rows,sorted(rows)) for name,rows in exacts.items()}

def ema(nums,period):
    alpha=2/(period+1)
    val=nums[0]
    return [((val:=alpha*x+(1-alpha)*val)) for x in nums]
# Compute indicator snapshot only from bars BEFORE the trade entry.
def indicators(rows,t):
    keys=sorted(x for x in rows if x<t)
    if len(keys)<50:return {}
    closes=[rows[k][3] for k in keys]
    e12=ema(closes,12);e26=ema(closes,26)
    hist=[a-b for a,b in zip(e12,e26)]
    sig=ema(hist,9)
    macd=[(v-s) for v,s in zip(hist,sig)]
    gains=[max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    losses=[max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag=sum(gains[-14:])/14;al=sum(losses[-14:])/14
    rsi=100 if al==0 else 100-100/(1+ag/al)
    return {"rsi14_simple":round(rsi,2),
            "ma10":round(statistics.mean(closes[-10:]),9),
            "ma10_dist_pct":round(100*(closes[-1]/statistics.mean(closes[-10:])-1),3),
            "hist_pct_price":round(100*macd[-1]/closes[-1],5),
            "hist_delta_pct_price":round(100*(macd[-1]-macd[-2])/closes[-1],5),
            "last_5_close_pct_change":round(100*(closes[-1]/closes[-6]-1),3)}

def one(inst,side,t):
    bars=exacts[inst];reg=state[inst]
    sign=1 if side=="LONG" else -1
    prev=bars[t-FIVE];entry=bars[t]
    original_color="LONG" if prev[3]>prev[0] else "SHORT" if prev[3]<prev[0] else "FLAT"
    median20=statistics.median(bars[t-FIVE-j*FIVE][4] for j in range(1,21))
    sigvol=prev[4];body=100*abs(prev[3]/prev[0]-1)
    evt={"inst":inst,"signal_ms":t,"side":side,"day":"2026-10-17"}
    actual=m.returns(bars,reg,evt,(.015,.01,12),t+16*FIVE)
    startp=entry[0]
    sl_level=startp*(.99 if sign==1 else 1.01)
    tp_level=startp*(1.015 if sign==1 else .985)
    candles=[]
    for j in range(12):
        k=t+j*FIVE
        b=bars.get(k)
        if not b:break
        o,h,l,c,v=b
        hit_sl=l<=sl_level if sign==1 else h>=sl_level
        hit_tp=h>=tp_level if sign==1 else l<=tp_level
        candles.append({"uk":uk(k),"open":o,"high":h,"low":l,"close":c,
                        "vol":round(v,6),"direction":"GREEN" if c>o else "RED" if c<o else "DOJI",
                        "sl_touch":hit_sl,"tp_touch":hit_tp,
                        "end_pct_move_direction":round(100*sign*(c/startp-1),3),
                        "ma10":round(statistics.mean([bars[k-z*FIVE][3] for z in range(10) if k-z*FIVE in bars]),9)})
        if hit_sl or hit_tp:break
    lookback=[]
    for j in range(6,0,-1):
        k=t-j*FIVE
        b=bars.get(k)
        if b:
            lookback.append({"at":uk(k),"color":"GREEN" if b[3]>b[0] else "RED" if b[3]<b[0] else "FLAT",
                             "body_pct":round(100*(b[3]/b[0]-1),3),
                             "volume":round(b[4],6),"vol_over_median20":round(b[4]/median20,2) if median20 else None})
    # Changes to within candle last 15 before signal, and stop excursion
    signal_position=(prev[3]-prev[2])/(prev[1]-prev[2]) if prev[1]!=prev[2] else None
    print("TRADE_DIAGNOSTIC="+json.dumps({
        "inst":inst,"side":side,"entry_uk":uk(t),"entry_price":startp,
        "signal_uk":uk(t-FIVE),"signal_color":original_color,
        "signal_candle":{ "open":prev[0],"high":prev[1],"low":prev[2],"close":prev[3],"volume":sigvol,
                          "body_pct":round(body,3),"close_position_of_range_0_to_1":round(signal_position,3) if signal_position is not None else None,
                          "volume_multiple":round(sigvol/median20,2) if median20 else None,
                          "volume_prev_ratio":round(sigvol/bars[t-2*FIVE][4],2) if bars[t-2*FIVE][4] else None
                        },
        "regime_signal":reg.get(t-FIVE),
        "signal_indicators":indicators(bars,t),
        "sl_price":sl_level,"tp_price":tp_level,"exit":actual,
        "last_six_signal_area_candles":lookback,
        "after_entry_bars_to_first_tp_sl":candles
    },ensure_ascii=False,separators=(",",":")),flush=True)

for inst,side,ts in wanted:
    one(inst,side,ts)
