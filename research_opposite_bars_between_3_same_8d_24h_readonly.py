"""READ ONLY. Explain losses of 3 same-colour consecutive volume exit.

Compare history of 24h and 8 independent previous days: TOP200 point-in-time,
5m market data only (archived, no network requests), same LONG/SHORT MA20
flip entries, 1x 25 USDT four-slot portfolio, no commissions or slippage.

Metrics computed causally per actual trade under 3-consecutive exit:
 total opposite 5m bars between entry and exit, longest uninterrupted opposite
 run, number of strong opposite bars (> entry signal volume), 0/1/2/... bins.
 Counterfactual stop exits at 1/2/3/4/5 opposite *consecutive* bars OR
 1/2/3/5 total opposite bars since entry, whichever arrives first before
 original 3 direction bars. All sells at next 5m open after confirmed close.
 Cross-check original 3-consecutive no-fee backtests before reporting.
 DO NOT CHANGE LIVE TRADING.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from pathlib import Path
import json, statistics
import short60_volume_mean10_24hour_readonly as frozen
import research_three_same_colour_exit_top200_24h_readonly as prev
import research_three_same_colour_exit_top200_8d_readonly as long8
import research_compare_flip24_early_same_top200_readonly as archived
import research_volume_bar_counts_top200_20261018_readonly as q

F=300000
prev.FEES=0.
long8.COST=0.
CAP=25.
CASES=("BASE3","STREAK1","STREAK2","STREAK3","STREAK4","STREAK5","TOTAL1","TOTAL2","TOTAL3","TOTAL5")

def bracket(n):
    if n==0:return "0"
    if n==1:return "1"
    if n==2:return "2"
    if n==3:return "3"
    if n==4:return "4"
    if n==5:return "5"
    return "6+"

def trade_stat(arr):
    n=len(arr)
    if n==0:return {"n":0}
    won=[x for x in arr if x["gross_pct"]>0]
    pnl=sum(x["money_usdt"] for x in arr)
    return {"n":n,"won":len(won),"lost":n-len(won),
      "win_pct":round(100*len(won)/n,2),
      "avg_gross_pct":round(sum(x["gross_pct"] for x in arr)/n,4),
      "total_profit_usdt":round(pnl,5),
      "average_hold_mins":round(statistics.mean(x["hold_bars"] for x in arr)*5,2)}

def prepare_candidates(market,ranks,start,end):
    """Cache all causal signal matches; be careful not to use future ranks."""
    result={}
    for prevts in range(start-F,end-F,F):
        here=[]
        for inst,rank in ranks[prevts].items():
            sig=q.qualify_entry(market[inst],prevts,True)
            nxt=market[inst].get(prevts+F)
            if sig and nxt and nxt[0]>0:
                here.append((rank,inst,sig,nxt[0],market[inst][prevts][4]))
        result[prevts]=sorted(here)
    return result

def simulate(market,ranks,start,end,mode,candidates):
    cash=CAP
    pos={}
    complete=[]
    opened=Counter()
    for tick in range(start,end,F):
        prior=tick-F
        # Exit at confirmed bar's NEXT OPEN. For each trade, same-colour
        # and opposite-colour counters update in chronological order.
        for inst,p in list(pos.items()):
            if prior<=p["signal_ts"]:continue
            bar=market[inst].get(prior)
            nxt=market[inst].get(tick)
            if bar is None or nxt is None:continue
            c=q.color(bar)
            if c==p["color"]:
                p["opp_streak"]=0
            elif c is not None:
                p["opposite_total"]+=1
                p["opp_streak"]+=1
                p["max_opp_streak"]=max(p["max_opp_streak"],p["opp_streak"])
                if bar[4]>p["entry_volume"]:
                    p["strong_opposite_total"]+=1
            base_signal=prev.indicator(p,bar,"AFTER_3_CONSECUTIVE")
            extra_signal=False
            if mode.startswith("STREAK"):
                extra_signal=p["opp_streak"]>=int(mode[6:])
            elif mode.startswith("TOTAL"):
                extra_signal=p["opposite_total"]>=int(mode[5:])
            exit_reason="3_same_consecutive" if base_signal else "cut_opposites"
            if base_signal or extra_signal:
                t=prev.result(p,tick,nxt[0])
                for field in ("opposite_total","max_opp_streak","strong_opposite_total"):
                    t[field]=p[field]
                t["exit_reason"]=exit_reason
                complete.append(t)
                cash+=t["money_usdt"]
                del pos[inst]
        # Only TOP200 ranks and genuine entry signals existing at prior candle close.
        for rank,inst,sig,price,vol in candidates.get(prior,()):
            if inst in pos:continue
            if len(pos)>=4:break
            available=min(cash,CAP)-sum(z["notional"] for z in pos.values())-.5
            if available<5:break
            stake=min(20,available/2)
            if stake<5:stake=min(20,available)
            if stake<5:break
            p=prev.position(inst,prior,sig,price,stake)
            p["entry_volume"]=vol
            p["opposite_total"]=0
            p["opp_streak"]=0
            p["max_opp_streak"]=0
            p["strong_opposite_total"]=0
            pos[inst]=p
            opened[p["side"]]+=1
    # For fair 8d vs 24h evaluation, mark leftover at end close
    # without claiming that the broker executed a trade.
    for inst,p in pos.items():
        bar=market[inst].get(end-F)
        if not bar:raise RuntimeError(f"Missing end candle {inst}")
        t=prev.result(p,end,bar[3],forced=True)
        for field in ("opposite_total","max_opp_streak","strong_opposite_total"):
            t[field]=p[field]
        t["exit_reason"]="period_end"
        complete.append(t)
        cash+=t["money_usdt"]
    return {
      "trade_count":len(complete),
      "positions_open_at_end":len(pos),
      "closed_by_three_streak":sum(x["exit_reason"]=="3_same_consecutive" for x in complete),
      "closed_by_opposite_cut":sum(x["exit_reason"]=="cut_opposites" for x in complete),
      "return_pct_25":round((cash-CAP)/CAP*100,3),
      "profit_usdt":round(cash-CAP,5),
      "wins":sum(x["win"] for x in complete),
      "losses":sum(not x["win"] for x in complete),
      "win_pct":round(100*sum(x["win"] for x in complete)/len(complete),2) if complete else None,
    },complete

def summarize_bins(trades):
    settled=[x for x in trades if not x["forced"]]
    out={"number_settled":len(settled),"number_open_at_end":len(trades)-len(settled)}
    for field in ("opposite_total","max_opp_streak","strong_opposite_total"):
        grouped=defaultdict(list)
        for t in settled:
            grouped[bracket(t[field])].append(t)
        out[field] = {bucket:trade_stat(grouped[bucket])
          for bucket in ("0","1","2","3","4","5","6+")}
        out[field]["at_least_1"]=trade_stat([x for x in settled if x[field]>=1])
        out[field]["at_least_2"]=trade_stat([x for x in settled if x[field]>=2])
        out[field]["at_least_3"]=trade_stat([x for x in settled if x[field]>=3])
        out[field]["at_least_5"]=trade_stat([x for x in settled if x[field]>=5])
    out["by_side"]={
        side: {
          "none_opposite":trade_stat([x for x in settled if x["side"]==side and x["opposite_total"]==0]),
          "opposite_streak_1":trade_stat([x for x in settled if x["side"]==side and x["max_opp_streak"]==1]),
          "opposite_streak_2":trade_stat([x for x in settled if x["side"]==side and x["max_opp_streak"]==2]),
          "opposite_streak_at_least_3":trade_stat([x for x in settled if x["side"]==side and x["max_opp_streak"]>=3]),
        }
        for side in ("LONG","SHORT")
    }
    out["worst_5_trades"] = sorted(settled,key=lambda x:x["gross_pct"])[:5]
    return out

def analyze(label,market,ranks,start,end,verify_return,verify_trades):
    all_candidates=prepare_candidates(market,ranks,start,end)
    result={}
    trades=None
    for mode in CASES:
        item,details=simulate(market,ranks,start,end,mode,all_candidates)
        result[mode]=item
        if mode=="BASE3":trades=details
    base=result["BASE3"]
    if base["trade_count"]!=verify_trades or abs(base["return_pct_25"]-verify_return)>.015:
        raise RuntimeError(f"Baseline did not replicate original no-fee {label}: {base} expected {verify_trades}, {verify_return}pct")
    return {"period":label,"fee_pct":0,"top200":200,"results":result,
            "breakdown_for_3_consecutive_strategy":summarize_bins(trades)}

def main():
    market,errors,total=frozen.load()
    if errors or len(market)!=390:raise RuntimeError("Missing archived 24h candles")
    ranks,coverage=frozen.rankings(market)
    one=analyze("2026-10-17 18:00 to 2026-10-18 18:00 UK",
      market,ranks,frozen.START+F,frozen.END,8.004,90)
    past=archived.load()
    if len(past)!=390:raise RuntimeError("Missing archived 8d candles")
    r8=long8.rankings(past)
    eight=analyze("2026-10-09 00:00 to 2026-10-17 00:00 UK",
      past,r8,long8.START,long8.END,25.971,623)
    report={"settings":"0 commission, 0 slippage, 1x, $25 portfolio, 4 positions, frozen TOP200 5m dynamic ranks, MA20 entry filter",
      "three_consecutive": "count opening signal candle as first; any reverse candle resets streak to 0; no other exit",
      "opposite_counts":"bars while position was open, after signal candle but before 3rd same-colour signal",
      "exit_counterfactual":"3 consecutive same-colour bars OR threshold of opposite bars, whichever first, execute at next 5m candle open",
      "causality_warning":"Bucket counts are observed over the whole trade and DO NOT prove causation; longer positions mechanically see more counter-trend bars; intervention variants re-run entire portfolio",
      "24h":one,"8d":eight}
    Path("opposite_bars_between_3_same_profit_loss_8d_24h.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    compact={key:{"results":{k:v for k,v in report[key]["results"].items()},
      "by_opposite_total":report[key]["breakdown_for_3_consecutive_strategy"]["opposite_total"],
      "by_max_opposite_streak":report[key]["breakdown_for_3_consecutive_strategy"]["max_opp_streak"],
      "by_strong_opposite":report[key]["breakdown_for_3_consecutive_strategy"]["strong_opposite_total"],
      "by_side":report[key]["breakdown_for_3_consecutive_strategy"]["by_side"]}
      for key in ("24h","8d")}
    print("OPPOSITE_BARS_LOSS_ANALYSIS="+json.dumps(compact,ensure_ascii=False,separators=(",",":")),flush=True)

if __name__=="__main__":main()
