"""READ ONLY independent eight-day sample (Oct 9–17 UK).

Validates the preceding 24h comparison of current LIVE exit versus
three SAME-COLOUR (total or consecutive) closed 5m bars using complete
public historic BloFin swaps archived in 39074940517. No API calls, no orders.
Point-in-time TOP200, 1x shared $25 capital, max 4 positions, MA20 entry,
no TP/SL; next 5m open fills; costs 0.12% fees; extra 0.10% cost stress.
Daily mark-to-market equity after estimated liquidation commission.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime
from pathlib import Path
import json, statistics
from zoneinfo import ZoneInfo
import research_compare_flip24_early_same_top200_readonly as archive
import research_three_same_colour_exit_top200_24h_readonly as test
import research_volume_bar_counts_top200_20261018_readonly as q
F=300000
START=archive.START
END=archive.END
UK=ZoneInfo("Europe/London")
COST=test.FEES
MODES=("CURRENT","AFTER_3_TOTAL","AFTER_3_CONSECUTIVE","FIRST_OF_BOTH","FIRST_OF_BOTH_CONSECUTIVE")

def stamp(ts):
    return datetime.fromtimestamp(ts/1000,UK).strftime("%Y-%m-%d %H:%M")
def day(ts):
    return datetime.fromtimestamp(ts/1000,UK).strftime("%Y-%m-%d")

def rankings(market):
    ranks={}
    for n,ts in enumerate(range(START-F,END,F)):
        candidates=[]
        prev=ts-288*F
        for inst,rows in market.items():
            c,b=rows.get(ts),rows.get(prev)
            if c and b and b[3]>0:
                candidates.append((c[3]/b[3]-1,inst))
        if len(candidates)<200:raise RuntimeError(f"TOP200 coverage insufficient on {stamp(ts)}: {len(candidates)}")
        candidates.sort(key=lambda pair:(-pair[0],pair[1]))
        ranks[ts]={inst:i for i,(_,inst) in enumerate(candidates[:200],1)}
    return ranks

def mark_equity(money,openpos,market,tick,extra_slippage=0):
    unrealized=0.0
    for inst,pos in openpos.items():
        bar=market[inst].get(tick)
        if bar is None:bar=market[inst].get(tick-F)
        px=(bar[0] if bar and tick in market[inst] else bar[3] if bar else pos["entry_price"])
        pct=(px/pos["entry_price"]-1)*100*(1 if pos["side"]=="LONG" else -1)
        unrealized+=pos["notional"]*(pct-COST-extra_slippage)/100
    return money+unrealized

def simulate(market,ranks,mode):
    cash=25.0
    positions={}
    closed=[]
    opened=Counter()
    daily=[]
    prior_equity=25.0
    last_peak=25.0
    max_drawdown=0.0
    for tick in range(START,END,F):
        prev=tick-F
        # Close only after 5m candle completed, never act on incomplete bars.
        for inst,pos in list(positions.items()):
            if prev<=pos["signal_ts"]:continue
            bar=market[inst].get(prev)
            nxt=market[inst].get(tick)
            if bar and nxt and test.indicator(pos,bar,mode):
                done=test.result(pos,tick,nxt[0])
                closed.append(done)
                cash+=done["money_usdt"]
                del positions[inst]
        for rank,inst in sorted((rank,inst) for inst,rank in ranks[prev].items()):
            if inst in positions:continue
            signal=q.qualify_entry(market[inst],prev,True)
            bar=market[inst].get(tick)
            if not signal or not bar or bar[0]<=0:continue
            if len(positions)>=4:break
            budget=min(cash,25.0)-sum(p["notional"] for p in positions.values())-.5
            if budget<5:break
            stake=min(20,budget/2)
            if stake<5:stake=min(20,budget)
            if stake<5:break
            pos=test.position(inst,prev,signal,bar[0],stake)
            pos["entry_volume"]=market[inst][prev][4]
            positions[inst]=pos
            opened[pos["side"]]+=1
        # Midnight UTC? no - use UK true midnight and accrued mark-to-market.
        next_ts=tick+F
        if next_ts<END and day(next_ts)!=day(tick):
            equity=mark_equity(cash,positions,market,next_ts)
            change=equity-prior_equity
            daily.append({"day_UK":day(tick),"pnl_usdt":round(change,5),
                          "daily_return_pct":round(change/prior_equity*100,3),
                          "end_equity_usdt":round(equity,5),
                          "open_positions_at_midnight":len(positions)})
            prior_equity=equity
            last_peak=max(last_peak,equity)
            max_drawdown=min(max_drawdown,(equity/last_peak-1)*100)
    for inst,pos in positions.items():
        candle=market[inst][END-F]
        done=test.result(pos,END,candle[3],forced=True)
        closed.append(done);cash+=done["money_usdt"]
    net=cash-25
    daily.append({"day_UK":day(END-F),"pnl_usdt":round(net-(prior_equity-25),5),
                  "daily_return_pct":round((cash-prior_equity)/prior_equity*100,3),
                  "end_equity_usdt":round(cash,5),
                  "open_positions_at_midnight":0})
    return {"entries":sum(opened.values()),"by_side":dict(opened),
            "closed_by_rule":len(closed)-len(positions),
            "positions_open_at_end":len(positions),
            "net_USDT":round(net,4),
            "total_return_pct_on_25":round(net/25*100,3),
            "total_if_extra_slippage_0_1_pct":round((net-sum(x["notional"]*.001 for x in closed))/25*100,3),
            "profit_days":sum(d["pnl_usdt"]>0 for d in daily),
            "days_over_3_pct":sum(d["daily_return_pct"]>=3 for d in daily),
            "max_daily_mark_to_market_drawdown_pct":round(max_drawdown,3),
            "daily":daily,
            "trades":test.metrics(closed,CAP) if False else {
                "count":len(closed),"win_pct":round(100*sum(x["win"] for x in closed)/len(closed),2),
                "mean_net_pct":round(statistics.mean(x["net_pct"] for x in closed),4)
            }}

def main():
    market=archive.load()
    if len(market)!=390:raise RuntimeError("Expected complete archive of 390 swaps")
    rank=rankings(market)
    results={mode:simulate(market,rank,mode) for mode in MODES}
    report={"period_UK":f"{stamp(START)} to {stamp(END)}",
            "markets":len(market),"ranking":"dynamic TOP200 every completed 5m candle",
            "exit_signal_count_entry_bar_is_first":True,
            "fees_total_pct":COST,"extra_slippage_sensitivity_pct":.1,
            "study":"no private API or actual fills; market open prices and notional estimates; 8 independent historical days",
            "results":results}
    Path("three_colour_exit_8day_independent_result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print("THREE_COLOUR_8DAY_RESULT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
if __name__=="__main__":main()
