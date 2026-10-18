"""READ ONLY: compare MA20 volume-flip LIVE exit with 3 same-colour bars.

2026-10-17 18:00 to 2026-10-18 18:00 UK, 390 frozen symbols, point-in-time TOP200.
Entries: exactly current LIVE 5m flip, two own MA20-qualified consecutive opposite
candles, last candle larger volume. Enter after signal candle closes, next open.
Strategies: current opposite-volume, 3 same-colour bars INCLUDING entry (not
necessarily consecutive), first of both, 3 consecutive same-colour bars.
Trade exits at next 5m open AFTER exit candle fully closes. Remaining positions
marked to market at end of study; 0.12% taker fees roundtrip and sensitivity
additional 0.10% total slippage; no funding, no orderbook, no market API calls.
Both paired-entries and 4-slot/$25 bankroll simulations, 1x, one coin at a time.
DOES NOT modify, enable, or execute LIVE trading.
"""
from __future__ import annotations
from collections import Counter
from pathlib import Path
import json, math, statistics
import short60_volume_mean10_24hour_readonly as frozen
import research_volume_bar_counts_top200_20261018_readonly as q
F=q.F
START=q.START
END=q.END
FEES=.12
SLIP=.10
CAP=25.0
BUFFER=.50
STRATS=("CURRENT","AFTER_3_TOTAL","FIRST_OF_BOTH","AFTER_3_CONSECUTIVE","AFTER_3_ADDITIONAL")
LABELS={
"CURRENT":"volume-only (opposite candle > entry volume)",
"AFTER_3_TOTAL":"3 same-colour bars incl entry (not necessarily consecutive)",
"FIRST_OF_BOTH":"whichever first: 3 total same-colour bars or opposite larger volume",
"AFTER_3_CONSECUTIVE":"3 consecutive same-colour bars incl entry",
"AFTER_3_ADDITIONAL":"3 NEW same-colour bars after entry (4 incl entry)",
}

def indicator(pos,bar,mode):
    co=q.color(bar)
    direction=pos["color"]
    opposite=(co is not None and co!=direction and bar[4]>pos["entry_volume"])
    if co==direction:
        pos["total_count"]+=1
        pos["additional_count"]+=1
        pos["streak"]+=1
    else:
        pos["streak"]=0
    count3=pos["total_count"]>=3
    if mode=="CURRENT":return opposite
    if mode=="AFTER_3_TOTAL":return count3
    if mode=="FIRST_OF_BOTH":return count3 or opposite
    if mode=="AFTER_3_CONSECUTIVE":return pos["streak"]>=3
    if mode=="AFTER_3_ADDITIONAL":return pos["additional_count"]>=3
    raise ValueError(mode)

def position(inst,signal_ts,signal,entry_price,notional=5.0):
    return {"inst":inst,"signal_ts":signal_ts,"color":signal,
            "side":"LONG" if signal=="GREEN" else "SHORT",
            "entry_price":entry_price,"entry_volume":None,"total_count":1,
            "additional_count":0,"streak":1,"notional":notional}

def result(pos,close_ts,exit_price,forced=False):
    side=pos["side"];gross=(exit_price/pos["entry_price"]-1)*100*(1 if side=="LONG" else -1)
    net=gross-FEES
    money=pos["notional"]*net/100
    return {"inst":pos["inst"],"side":side,"signal_ts":pos["signal_ts"],
            "exit_ts":close_ts,"hold_bars":max(0,(close_ts-pos["signal_ts"]-F)//F),
            "gross_pct":gross,"net_pct":net,"net_with_slippage_pct":net-SLIP,
            "money_usdt":money,"notional":pos["notional"],"forced":forced,
            "win":net>0}

def metrics(trades,start_cap=None):
    closed=[x for x in trades if not x["forced"]]
    forced=[x for x in trades if x["forced"]]
    def view(xs):
        if not xs:return {"count":0}
        r=[x["net_pct"] for x in xs]
        by=[x["money_usdt"] for x in xs]
        return {"count":len(xs),"wins":sum(x["win"] for x in xs),
                "win_pct":round(100*sum(x["win"] for x in xs)/len(xs),2),
                "avg_net_pct":round(statistics.mean(r),4),
                "median_net_pct":round(statistics.median(r),4),
                "sum_net_pct":round(sum(r),3),
                "sum_usdt":round(sum(by),4),
                "avg_hold_minutes":round(statistics.mean(x["hold_bars"] for x in xs)*5,2),
                "net_if_slip_0_1pct_extra_usdt":round(sum(x["notional"]*(x["net_pct"]-SLIP)/100 for x in xs),4),
                "long":sum(x["side"]=="LONG" for x in xs),
                "short":sum(x["side"]=="SHORT" for x in xs)}
    total=view(trades)
    if start_cap is not None and total["count"]:
        total["portfolio_return_pct_on_25_USDT"]=round(total["sum_usdt"]/start_cap*100,3)
        total["portfolio_return_with_slip_pct"]=round(total["net_if_slip_0_1pct_extra_usdt"]/start_cap*100,3)
    return {"total_including_end_mark_to_market":total,"closed_by_rules":view(closed),
            "remaining_force_closed_at_period_end":view(forced)}

def paired_events(market,ranks):
    events=[]
    for ts in range(START,END-F,F):
        for inst in ranks[ts]:
            rows=market[inst]
            signal=q.qualify_entry(rows,ts,True)
            nxt=rows.get(ts+F)
            if signal and nxt and nxt[0]>0:
                events.append((ts,inst,signal))
    out={name:[] for name in STRATS}
    for ts,inst,signal in events:
        rows=market[inst]
        entry=rows[ts+F][0]
        for name in STRATS:
            pos=position(inst,ts,signal,entry)
            pos["entry_volume"]=rows[ts][4]
            exit_at=END
            exit_px=rows[END-F][3]
            forced=True
            for candle_ts in range(ts+F,END-F,F):
                bar=rows.get(candle_ts)
                nextbar=rows.get(candle_ts+F)
                if not bar or not nextbar:continue
                if indicator(pos,bar,name):
                    exit_at=candle_ts+F
                    exit_px=nextbar[0]
                    forced=False
                    break
            out[name].append(result(pos,exit_at,exit_px,forced))
    return events,out

def portfolio(market,ranks,name):
    positions={}
    closed=[]
    opened=Counter()
    reasons=Counter()
    money=CAP
    skipped_budget=0
    skipped_slots=0
    for tick in range(START+F,END,F):
        prior=tick-F
        # On the new candle open, process exit signal of the previous completed candle.
        for inst,pos in list(positions.items()):
            # Do not evaluate entry signal candle again.
            if prior<=pos["signal_ts"]:continue
            bar=market[inst].get(prior)
            price=market[inst].get(tick)
            if not bar or not price:continue
            if indicator(pos,bar,name):
                r=result(pos,tick,price[0])
                closed.append(r);money+=r["money_usdt"];del positions[inst]
        # Then enter current TOP200 candidates from the last fully closed bar.
        options=[]
        for inst,rank in ranks[prior].items():
            if inst in positions:continue
            signal=q.qualify_entry(market[inst],prior,True)
            price=market[inst].get(tick)
            if signal and price and price[0]>0:
                options.append((rank,inst,signal,price[0],market[inst][prior][4]))
        for rank,inst,signal,price,vol in sorted(options):
            if len(positions)>=4:
                skipped_slots+=1;break
            # 1x isolated shared investment budget, rough live sizing.
            available=min(money,CAP)-sum(v["notional"] for v in positions.values())-BUFFER
            if available<5:
                skipped_budget+=1;break
            amount=min(20,available/2)
            if amount<5:amount=min(20,available)
            if amount<5:break
            pos=position(inst,prior,signal,price,amount)
            pos["entry_volume"]=vol
            positions[inst]=pos
            opened[pos["side"]]+=1
    # Mark still-open trades at last published completed candle close, not
    # as executed broker closes; include estimated roundtrip fees for fairness.
    for inst,pos in positions.items():
        endbar=market[inst][END-F]
        closed.append(result(pos,END,endbar[3],True))
        reasons["period_end_open"]+=1
    return {"entries":sum(opened.values()),"entries_by_side":dict(opened),
            "period_end_open":len(positions),
            "skipped_budget":skipped_budget,"skipped_slots":skipped_slots,
            "metrics":metrics(closed,CAP),
            "first_5_trades":closed[:5]}

def main():
    market,errs,total=frozen.load()
    if len(market)!=390 or errs:raise RuntimeError("Expected complete 390-futures archive")
    ranks,coverage=frozen.rankings(market)
    events,paired=paired_events(market,ranks)
    if not events:raise RuntimeError("No qualifying entry signals")
    report={
      "period_UK":f"{q.timestamp(START)} to {q.timestamp(END)}",
      "dataset":"frozen BloFin 390-coin 5m candles; every 5m point-in-time TOP200 ranking",
      "entry_causality":"next 5m open, both signal candles below previous 20-bar volume average",
      "third_candle_count_INCLUDES_entry":True,
      "entry_signal_count":len(events),
      "fees_pct_roundtrip":FEES,
      "slippage_sensitivity_pct_roundtrip_additional":SLIP,
      "limitations":"No broker fills, funding or exchange impact; 24h only; period-end trades marked to market; model no true exchange min-size/spread.",
      "same_entries_comparison":{name:{"label":LABELS[name],"metrics":metrics(paired[name])} for name in STRATS},
      "portfolio_25usdt_max4_1x_simulation":{name:{"label":LABELS[name],**portfolio(market,ranks,name)} for name in STRATS},
      "comparison_note":"Paired entries reuse all same entry signals regardless of concurrent capital. Portfolio simulation uses a maximum 4 slots and about 25 USDT capital; variants trade different subsequent symbols due to different holding durations."
    }
    Path("volume_exit_after_three_same_colour_24h.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    compact={
      "period_UK":report["period_UK"],
      "entry_signal_count":len(events),
      "same_entries":{k:{"total":v["metrics"]["total_including_end_mark_to_market"],"only_rule_closes":v["metrics"]["closed_by_rules"],"censored":v["metrics"]["remaining_force_closed_at_period_end"]["count"]} for k,v in report["same_entries_comparison"].items()},
      "portfolio":{k:{"entries":v["entries"],"still_open_at_end":v["period_end_open"],"metrics":v["metrics"]["total_including_end_mark_to_market"],"closed_by_rules":v["metrics"]["closed_by_rules"]} for k,v in report["portfolio_25usdt_max4_1x_simulation"].items()},
    }
    print("THREE_SAME_COLOUR_EXIT_BACKTEST="+json.dumps(compact,ensure_ascii=False,separators=(",",":")),flush=True)
if __name__=="__main__":main()
