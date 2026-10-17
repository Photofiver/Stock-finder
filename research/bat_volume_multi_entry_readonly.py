#!/usr/bin/env python3
"""Isolated historical research; never reads keys or calls BloFin; 10 config batches."""
import collections, datetime as dt, json, statistics, time
from pathlib import Path
from bat_volume_grid_readonly import source_data, MINIMUMS, MULTIPLES, MEMORIES, FIVE

TP=.01
SL=.0025
FEE=.0006
SIZE=20.
MIN_TRADES=20
METHODS=("FLIP_ONLY","NEW_RECORD","EVERY_VALID")
DIRECTIONS=("BOTH","LONG_ONLY","SHORT_ONLY")
PERIODS=(30,60,90)

def signals(rows, medians, floor, factor, memory):
    direction=0
    record=0.
    dq=collections.deque()
    out=[]
    prev_ts=None
    for i,(ts,op,hi,lo,cl,v) in enumerate(rows):
        if prev_ts is not None and ts!=prev_ts+FIVE:
            direction=0
            record=0.
            dq.clear()
        prev_ts=ts
        if i<50:
            continue
        if memory:
            while dq and dq[0][0] < i-memory+1:
                dq.popleft()
            record=dq[0][1] if dq else 0.
        median=medians[i]
        if not(cl!=op and v>=floor and not (factor and median>0 and v>factor*median)):
            continue
        side=1 if cl>op else -1
        flipped=False
        if direction==0:
            direction=side
            record=0.
            dq.clear()
            initial=True
        elif side!=direction:
            if v<=record:
                continue
            direction=side
            record=0.
            dq.clear()
            flipped=True
            initial=False
        else:
            initial=False
        is_record=v>record
        if memory:
            while dq and dq[-1][1]<=v:
                dq.pop()
            dq.append((i,v))
            record=dq[0][1]
        else:
            record=max(record,v)
        if not initial:
            out.append((i,side,flipped,is_record))
    return out

def simulate(rows, events, start, split, end, method, direction, first_part):
    begin,limit=(start,split) if first_part=="TRAIN" else (split,end)
    pnl=[]
    ambiguous=0
    busy=-1
    event_count=0
    for i,side,flipped,new_record in events:
        ts=rows[i][0]
        if not(begin<=ts<limit):continue
        if direction=="LONG_ONLY" and side!=1:continue
        if direction=="SHORT_ONLY" and side!=-1:continue
        if method=="FLIP_ONLY" and not flipped:continue
        if method=="NEW_RECORD" and not new_record:continue
        event_count+=1
        if i<busy or i+2>=len(rows):continue
        x,y=rows[i+1],rows[i+2]
        if x[0]!=ts+FIVE or y[0]!=ts+2*FIVE:continue
        if y[0]+FIVE>limit:continue
        entry=x[1]
        target=entry*(1+side*TP)
        stop=entry*(1-side*SL)
        price=y[4]
        exit_i=i+2
        hit_ambig=False
        for j,z in enumerate((x,y)):
            op,hi,lo=z[1],z[2],z[3]
            if (op<=stop if side==1 else op>=stop):
                price=op;exit_i=i+1+j;break
            if (op>=target if side==1 else op<=target):
                price=op;exit_i=i+1+j;break
            hit_sl=(lo<=stop if side==1 else hi>=stop)
            hit_tp=(hi>=target if side==1 else lo<=target)
            if hit_sl:
                price=stop;exit_i=i+1+j;hit_ambig=hit_tp;break
            if hit_tp:
                price=target;exit_i=i+1+j;break
        pl=SIZE*side*(price/entry-1) - FEE*SIZE*(1+price/entry)
        pnl.append((pl,side))
        ambiguous+=int(hit_ambig)
        busy=exit_i
    net=sum(t[0] for t in pnl)
    gains=sum(max(0,t[0]) for t in pnl)
    losses=sum(max(0,-t[0]) for t in pnl)
    wins=sum(t[0]>0 for t in pnl)
    return {"trades":len(pnl),"wins":wins,"losses":len(pnl)-wins,
            "win_pct":round(wins/len(pnl)*100,2) if pnl else None,
            "net_usdt":round(net,6),
            "pf":round(gains/losses,3) if losses else ("inf" if gains else None),
            "long_net":round(sum(p for p,d in pnl if d==1),6),
            "short_net":round(sum(p for p,d in pnl if d==-1),6),
            "ambiguous":ambiguous,"eligible_signals":event_count}

def main():
    t0=time.monotonic()
    rows,end,archives=source_data()
    endms=int(end.timestamp()*1000)
    medians=[0.]*len(rows)
    for i in range(50,len(rows)):
        medians[i]=statistics.median(r[5] for r in rows[i-50:i])
    specs=[(floor,factor,memory) for floor in MINIMUMS for factor in MULTIPLES for memory in MEMORIES]
    report={"period_end":end.isoformat(),"symbol":"BINANCE:BATUSDT",
            "source_bars":len(rows),"archive_failures":archives,
            "method_definitions":{"FLIP_ONLY":"Only trend reversal","NEW_RECORD":"Every accepted same-direction candle exceeding prior trend record, plus reversals","EVERY_VALID":"Every accepted candle whose color matches established trend, plus reversals"},
            "assumptions":{"fee_per_side_pct":.06,"TP_pct":1,"SL_pct":.25,
                           "trade_size_usdt":20,"deadline_min":10,
                           "open_trade_limit":1,"entry":"following bar open",
                           "SL_TP_same_bar":"SL first","signals":"closed bars only",
                           "training_selection":"top TRAIN net with at least 20 TRAIN trades per window/method/direction",
                           "caution":"5m bars cannot resolve actual order of TP/SL; figures ignore spread/slippage"},
            "configurations":len(specs),"batch_size":10,"results":[]}
    # time-sliced period views, no repeated download
    periods={}
    for days in PERIODS:
        start=endms-days*86400000
        split=start+int(days*.7*86400000//FIVE)*FIVE
        periods[days]=(start,split,endms)
    for startidx in range(0,len(specs),10):
        group=specs[startidx:startidx+10]
        for floor,factor,memory in group:
            evt=signals(rows,medians,floor,factor,memory)
            stat={}
            for days,(start,split,endms0) in periods.items():
                for method in METHODS:
                    for direction in DIRECTIONS:
                        key=f"{days}|{method}|{direction}"
                        tr=simulate(rows,evt,start,split,endms0,method,direction,"TRAIN")
                        te=simulate(rows,evt,start,split,endms0,method,direction,"TEST")
                        stat[key]={"train":tr,"test":te}
            report["results"].append({"floor":floor,"factor":factor,"memory":memory,"statistics":stat})
        print("BATCH_DONE",startidx//10+1,"CONFIGS",startidx+1,"-",startidx+len(group),"TOTAL",len(specs),"SECONDS",round(time.monotonic()-t0,1),flush=True)
    outcomes=[]
    for days in PERIODS:
        for method in METHODS:
            for direction in DIRECTIONS:
                key=f"{days}|{method}|{direction}"
                qualified=[x for x in report["results"] if x["statistics"][key]["train"]["trades"]>=MIN_TRADES]
                qualified.sort(key=lambda x:(-x["statistics"][key]["train"]["net_usdt"],-x["statistics"][key]["train"]["trades"],x["floor"],x["factor"],x["memory"]))
                winner=qualified[0] if qualified else None
                if winner is not None:
                    piece={"days":days,"method":method,"direction":direction,
                           "chosen_filter":{"floor":winner["floor"],"factor":winner["factor"],"memory":winner["memory"]},
                           **winner["statistics"][key],"qualified_count":len(qualified)}
                else:piece={"days":days,"method":method,"direction":direction,"qualified_count":0}
                outcomes.append(piece)
                print("SELECTED_RESULT="+json.dumps(piece,separators=(",",":")),flush=True)
    report["selected_winners"]=outcomes
    # Retrospective stability scan, explicitly not independent because looking at all windows.
    stable=[]
    for method in METHODS:
      for direction in DIRECTIONS:
        for x in report["results"]:
          values=[x["statistics"][f"{d}|{method}|{direction}"] for d in PERIODS]
          if all(v["train"]["trades"]>=MIN_TRADES and v["test"]["trades"]>=10 and v["train"]["net_usdt"]>0 and v["test"]["net_usdt"]>0 for v in values):
              stable.append({"floor":x["floor"],"factor":x["factor"],"memory":x["memory"],
                             "method":method,"direction":direction,
                             "net_test":[v["test"]["net_usdt"] for v in values],
                             "trades_test":[v["test"]["trades"] for v in values],
                             "win_pct_test":[v["test"]["win_pct"] for v in values]})
    report["retrospectively_stable_not_independent"]=stable
    print("RETROSPECTIVE_STABLE_COUNT",len(stable),flush=True)
    for x in sorted(stable,key=lambda x:min(x["net_test"]),reverse=True)[:20]:
        print("RETROSPECTIVE="+json.dumps(x,separators=(",",":")),flush=True)
    report["elapsed_seconds"]=round(time.monotonic()-t0,1)
    path=Path("research/bat_volume_multi_entry_results.json")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    print("SAVED",path,"SECONDS",report["elapsed_seconds"],flush=True)

if __name__=="__main__":main()
