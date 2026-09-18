import json, time
from collections import defaultdict
import blofin_live_hourly as bot

DAYS = 30
HOUR = 60*60*1000
MIN = 60*1000
HOLD_MS = 5*HOUR
FEE_RT_PCT = 0.12
TPS = [0.4,0.5,0.6,0.7,0.8,1.0,1.2,1.5]
SLS = [0.3,0.4,0.5,0.6,0.75,1.0]

def fetch_1h_long(inst):
    raw = bot.market_get("/api/v1/market/candles", {"instId":inst,"bar":"1H","limit":"900"})
    return bot.decorate(bot.parse_candles(raw))

print("Loading universe...")
_,_,metas = bot.get_universe()
insts = sorted(metas.keys())
print("UNIVERSE", len(insts))

series = {}
for n,inst in enumerate(insts,1):
    try:
        b = fetch_1h_long(inst)
        if len(b) >= 50:
            series[inst] = b
    except Exception as e:
        print("1HERR",inst,type(e).__name__)
    if n % 50 == 0:
        print("1H_PROGRESS",n,len(series))
        time.sleep(0.2)

latest_close = max((b[-1]["ts"]+HOUR for b in series.values()), default=0)
end_close = (latest_close//HOUR)*HOUR
start_close = end_close - DAYS*24*HOUR
hours = list(range(start_close, end_close, HOUR))
print("PERIOD", start_close, end_close, "hours", len(hours), "series", len(series))

maps = {}
for inst,bars in series.items():
    maps[inst] = {int(x["ts"]+HOUR):(i,x) for i,x in enumerate(bars)}

def volume_side(bars,i):
    if i < 1: return None
    cc = bot.candle_color(bars[i])
    if cc=="GREEN" and bot.volume_ok(bars,i,"LONG"): return "LONG"
    if cc=="RED" and bot.volume_ok(bars,i,"SHORT"): return "SHORT"
    return None

hour_candidates = {}
all_events = set()
hours_with_rank = 0
for k,close_ms in enumerate(hours,1):
    ranked=[]
    prev_ms = close_ms - 24*HOUR
    for inst,bars in series.items():
        curpair = maps[inst].get(close_ms)
        prevpair = maps[inst].get(prev_ms)
        if not curpair or not prevpair: continue
        cur = curpair[1]; prev = prevpair[1]
        if prev["c"] <= 0: continue
        ret = cur["c"]/prev["c"] - 1.0
        ranked.append((ret,inst,curpair[0]))
    ranked.sort(reverse=True)
    top10=ranked[:10]
    if top10: hours_with_rank += 1
    cands=[]
    for rank,(_,inst,i) in enumerate(top10,1):
        side=volume_side(series[inst],i)
        if side:
            cands.append((rank,inst,side,close_ms))
            all_events.add((inst,side,close_ms))
    hour_candidates[close_ms]=cands
    if k%100==0: print("SIGNAL_PROGRESS",k,"events",len(all_events))

print("CANDIDATE_EVENTS",len(all_events),"hours_ranked",hours_with_rank)

minute_cache={}
def get_window(inst,start):
    key=(inst,start)
    if key in minute_cache: return minute_cache[key]
    end=start+HOLD_MS+MIN
    out=[]
    try:
        raw=bot.market_get("/api/v1/market/candles",{
            "instId":inst,"bar":"1m","after":str(end),"limit":"380"
        })
        for r in raw:
            try:
                if len(r)>=9 and str(r[8])=="1":
                    ts=int(r[0])
                    if start <= ts <= start+HOLD_MS:
                        out.append({"ts":ts,"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4])})
            except Exception:
                pass
        out.sort(key=lambda x:x["ts"])
    except Exception as e:
        print("1MERR",inst,start,type(e).__name__)
    minute_cache[key]=out
    return out

events=list(all_events)
for n,(inst,side,start) in enumerate(events,1):
    get_window(inst,start)
    if n%50==0:
        print("1M_PROGRESS",n,"of",len(events))
        time.sleep(0.2)

def event_outcome(inst,side,start,tp_pct,sl_pct):
    bs=minute_cache.get((inst,start),[])
    if not bs:
        return None
    entry=bs[0]["o"]
    tp=entry*(1+tp_pct/100) if side=="LONG" else entry*(1-tp_pct/100)
    sl=entry*(1-sl_pct/100) if side=="LONG" else entry*(1+sl_pct/100)
    for b in bs:
        hit_tp = b["h"]>=tp if side=="LONG" else b["l"]<=tp
        hit_sl = b["l"]<=sl if side=="LONG" else b["h"]>=sl
        if hit_tp and hit_sl:
            return {"gross":-sl_pct,"exit":b["ts"],"ambig":True,"kind":"SL_CONSERVATIVE"}
        if hit_sl:
            return {"gross":-sl_pct,"exit":b["ts"],"ambig":False,"kind":"SL"}
        if hit_tp:
            return {"gross":tp_pct,"exit":b["ts"],"ambig":False,"kind":"TP"}
    last=bs[-1]
    raw=((last["c"]/entry)-1)*100 if side=="LONG" else ((entry/last["c"])-1)*100
    return {"gross":raw,"exit":last["ts"]+MIN,"ambig":False,"kind":"MAX5H"}

results=[]
for tp in TPS:
  for sl in SLS:
    position_until=0
    trades=[]
    for close_ms in hours:
        if close_ms < position_until: continue
        cands=hour_candidates.get(close_ms) or []
        if not cands: continue
        rank,inst,side,start=cands[0]
        o=event_outcome(inst,side,start,tp,sl)
        if not o: continue
        net=o["gross"]-FEE_RT_PCT
        trades.append((net,o["gross"],o["kind"],o["ambig"],inst,side,start,rank,o["exit"]))
        position_until=o["exit"]
    wins=sum(1 for x in trades if x[0]>0)
    losses=sum(1 for x in trades if x[0]<0)
    amb=sum(1 for x in trades if x[3])
    net=sum(x[0] for x in trades)
    gross=sum(x[1] for x in trades)
    n=len(trades)
    results.append({
      "tp":tp,"sl":sl,"trades":n,"wins":wins,"losses":losses,"ambiguous":amb,
      "win_rate":round(100*wins/n,2) if n else 0,
      "gross_pct":round(gross,3),"net_pct":round(net,3),
      "avg_net_pct":round(net/n,4) if n else 0
    })

results.sort(key=lambda x:(x["net_pct"],x["trades"]), reverse=True)
print("TOP_RESULTS "+json.dumps(results[:15],sort_keys=True))
current=next(x for x in results if x["tp"]==0.6 and x["sl"]==0.5)
print("CURRENT_06_05 "+json.dumps(current,sort_keys=True))
positive=[x for x in results if x["net_pct"]>0]
print("POSITIVE_COUNT",len(positive))
print("SUMMARY "+json.dumps({
 "days":DAYS,"hours":len(hours),"universe":len(series),"candidate_events":len(all_events),
 "fee_roundtrip_pct":FEE_RT_PCT,"positive_configs":len(positive),
 "best":results[0] if results else None,"current":current
},sort_keys=True))
