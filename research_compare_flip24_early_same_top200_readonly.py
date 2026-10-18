"""READ ONLY: apples-to-apples EARLY vs FLIP+HOLD_VOLUME strategies.

Dataset: existing VERIFIED 390 BloFin USDT-crypto-swaps 5-minute candle
shards from 2026-10-09 00:00 UK through 2026-10-17 00:00 UK, 8 full UK
days. Reconstruct point-in-time TOP200 from the prior 24h closed-candle
percent gain among the exact same 390 current live instruments. No hindsight
symbol selection, no live API or credential access.
Both use a SINGLE global 20 USDT position and 25 USDT starting equity;
signal after 5-minute close, execute at next 5-minute open. Rank ascending
then ticker alphabetical if multiple same-time events.

EARLY = <=3 signal candles since strict peak volume regime flip,
same directional candle, volume>prior candle, volume>=4x median20,
candle body >=0.4%, signal between 12:00 and 18:00 UK, TP1.5% SL1%
or 60 minutes or opposing volume regime flip.
FLIP24 = strict peak-volume regime FLIP candle itself, volume>=2x median20,
body>=0.1%, either LONG/SHORT, 24h per trade or opposing STRICT flip, no
price TP/SL. Reset reference peak at UK midnight.
Cost both: 0.12% estimated roundtrip fees plus 0.10% adverse slippage
roundtrip; FLIP24 additionally adverse funding 0.01% per elapsed 8 hours.
Funding is an assumption, NOT pulled from historic funding exchange records.
Compare daily mark-to-market equity vs fixed starting 25 USDT, gross/closed
trades, daily positive days, drawdown on 5m close (not intrabar wicks), and
8 day net returns. Force end-of-history liquidation for fair equality.
Full 45-day data from pasted report is not present in this 8-day dataset.
This is research only. DO NOT change LIVE.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict
from itertools import groupby
import gzip, json, math, statistics

import blofin_top200_repeat_setup_research as m

FIVE=m.FIVE
UK=ZoneInfo("Europe/London")
FIRST=datetime(2026,10,9,0,0,tzinfo=UK)
ENDDT=datetime(2026,10,17,0,0,tzinfo=UK)
START=int(FIRST.timestamp()*1000)
END=int(ENDDT.timestamp()*1000)
N_DAYS=8
DAYS=[(FIRST+timedelta(days=i)).date().isoformat() for i in range(N_DAYS)]
NOTIONAL=20.
CAPITAL=25.
COST=0.22
FUNDING_8H_EST=0.01
EIGHT_H=8*60*60*1000
ONE_DAY=24*60*60*1000

def uk(ts):
    return datetime.fromtimestamp(ts/1000,UK)
def stamp(ts):
    return uk(ts).strftime("%Y-%m-%d %H:%M")

def load():
    raw={}
    universe=[]
    missing={}
    for i in range(4):
        path=Path("shards")/f"shard_{i}.json.gz"
        with gzip.open(path,"rt",encoding="utf-8") as f:x=json.load(f)
        assert x["asof_uk"]=="2026-10-17"
        if x["all_symbol_count"]!=390:raise RuntimeError("Expected exact verified 390 instrument archive")
        universe.extend(x["symbols_in_shard"])
        missing.update(x["errors"])
        for inst,rows in x["candle_rows"].items():
            if inst in raw:raise RuntimeError("Duplicate coin "+inst)
            raw[inst]={int(t):tuple(b) for t,b in rows.items()}
    if len(raw)!=390 or len(set(universe))!=390 or missing:raise RuntimeError("Not 390 complete shards")
    return raw

def prepare(raw):
    st=min(min(x) for x in raw.values())
    keys=range(st-(st%FIVE),END,FIVE)
    state={inst:m.volume_states(bars,keys) for inst,bars in raw.items()}
    target_time={};
    early=[];
    flip=[];
    coverage=[]
    for i,ts in enumerate(range(START-FIVE,END-FIVE,FIVE),1):
        # Signal known AFTER 5-minute candle closes at ts+FIVE.
        # First available previous-day historical 24h gain for each coin.
        ranks=[]
        for inst,bars in raw.items():
            c=bars.get(ts)
            older=bars.get(ts-288*FIVE)
            if c and older and older[3]>0:
                ranks.append((c[3]/older[3]-1,inst))
        if len(ranks)<200:
            raise RuntimeError(f"Historical TOP200 is incomplete at {stamp(ts)} (only {len(ranks)})")
        ranks.sort(reverse=True)
        coverage.append(len(ranks))
        entry_time=ts+FIVE
        if entry_time<START or entry_time>=END:continue
        for rank,(_,inst) in enumerate(ranks[:200],1):
            bars=raw[inst]
            candle=bars[ts]
            a=state[inst].get(ts)
            if a is None:continue
            side,is_flip,peak=a
            if not side:continue
            ratio=None
            prev=[]
            for j in range(1,21):
                bb=bars.get(ts-FIVE*j)
                if bb is None:break
                prev.append(bb[4])
            if len(prev)<20:continue
            median=statistics.median(prev)
            if median<=0:continue
            ratio=candle[4]/median
            body=100*abs(candle[3]/candle[0]-1)
            color="LONG" if candle[3]>candle[0] else "SHORT" if candle[3]<candle[0] else "FLAT"
            record={"inst":inst,"signal_ms":entry_time,"signal_candle_ms":ts,
                    "side":side,"rank":rank,"ratio":ratio,"body_pct":body,
                    "entry_day":uk(entry_time).date().isoformat(),
                    "signal_candle_color":color}
            if is_flip and color==side and ratio>=2 and body>=0.1:
                flip.append(record)
            # EARLY signal exactly as the previous author's family rule in
            # m.calc_event -- it uses any strict flip on signal candle or
            # the preceding three candles, and volume>immediately previous.
            h=uk(entry_time).hour
            if h>=12 and h<18 and color==side and ratio>=4 and body>=0.4:
                previous=bars.get(ts-FIVE)
                if previous and candle[4]>previous[4]:
                    recent=any(state[inst].get(ts-FIVE*j,(None,False,None))[1]
                               for j in range(4))
                    if recent:
                        early.append(record)
        if i%400==0:
            print(f"RANKING_PROGRESS={i}/2304 EARLY={len(early)} FLIP24={len(flip)}",flush=True)
    early.sort(key=lambda x:(x["signal_ms"],x["rank"],x["inst"]))
    flip.sort(key=lambda x:(x["signal_ms"],x["rank"],x["inst"]))
    return state,early,flip,coverage

def compute_equity(base_cash,position,bars,t):
    if position is None:return base_cash
    coin=position["coin"]
    price=bars[coin].get(t-FIVE)
    if price is None:
        price=bars[coin].get(t)
    if price is None:raise RuntimeError("Missing candle for MTM "+coin+" "+stamp(t))
    px=price[3]
    signed=1 if position["side"]=="LONG" else -1
    pct=100*signed*(px/position["entry_price"]-1)
    estimated_funding=(max(0,t-position["start"])//EIGHT_H)*FUNDING_8H_EST if position["funded"] else 0.
    return base_cash+NOTIONAL*(pct-COST-estimated_funding)/100

def backtest(label,events,raw,states,mode):
    arrivals=defaultdict(list)
    for e in events:
        arrivals[e["signal_ms"]].append(e)
    equity=CAPITAL
    pos=None
    positions=[]
    daily={}
    opens=defaultdict(int)
    peak=CAPITAL
    maxdraw=0
    low_equity=CAPITAL
    exitcounts=defaultdict(int)
    sorted_events=sorted(events,key=lambda e:(e["signal_ms"],e["rank"],e["inst"]))
    # Clear persistent mark-equity snapshots and no per-day portfolio restart.
    for t in range(START,END,FIVE):
        # End-of-UK-day snapshot BEFORE this timestamp's actions. This
        # isolates previous day's returns and permits overnight carry.
        if t>START and uk(t).hour==0 and uk(t).minute==0:
            yesterday=(uk(t)-timedelta(minutes=1)).date().isoformat()
            daily[yesterday]={"equity_end_usdt":compute_equity(equity,pos,raw,t)}
        just_exited=False
        if pos:
            if mode=="EARLY":
                if t>=pos["exit_plan_ms"]:
                    net_pct=pos["exit_plan_gross_pct"]-COST
                    equity+=NOTIONAL*net_pct/100
                    positions.append({"coin":pos["coin"],"side":pos["side"],
                         "entry_uk":stamp(pos["start"]),"exit_uk":stamp(t),
                         "entry_day":pos["start_day"],"exit_day":uk(t).date().isoformat(),
                         "minutes_held":(t-pos["start"])/60000,
                         "exit":pos["exit_plan_reason"],"net_pct":round(net_pct,4),
                         "net_usdt":round(NOTIONAL*net_pct/100,5),
                         "source_volume_multiple":round(pos["ratio"],3),
                         "source_rank":pos["rank"]})
                    exitcounts[pos["exit_plan_reason"]]+=1
                    pos=None
                    just_exited=True
            else:
                coin=pos["coin"]
                current_state=states[coin].get(t-FIVE,(None,False,None))
                stopped=bool(current_state[1] and current_state[0]!=pos["side"])
                timed=(t-pos["start"]>=ONE_DAY)
                if stopped or timed:
                    entry_bar=raw[coin].get(t)
                    if entry_bar is None: raise RuntimeError("Missing exit open "+coin+" "+stamp(t))
                    sign=1 if pos["side"]=="LONG" else -1
                    gross_pct=100*sign*(entry_bar[0]/pos["entry_price"]-1)
                    funding_est=(max(0,t-pos["start"])//EIGHT_H)*FUNDING_8H_EST
                    net_pct=gross_pct-COST-funding_est
                    equity+=NOTIONAL*net_pct/100
                    why="OPPOSITE_CONFIRMED_FLIP" if stopped else "24H_TIME"
                    positions.append({"coin":coin,"side":pos["side"],
                         "entry_uk":stamp(pos["start"]),"exit_uk":stamp(t),
                         "entry_day":pos["start_day"],"exit_day":uk(t).date().isoformat(),
                         "minutes_held":(t-pos["start"])/60000,
                         "exit":why,"net_pct":round(net_pct,4),
                         "net_usdt":round(NOTIONAL*net_pct/100,5),
                         "estimated_funding_pct":round(funding_est,4),
                         "source_volume_multiple":round(pos["ratio"],3),
                         "source_rank":pos["rank"]})
                    exitcounts[why]+=1
                    pos=None
                    just_exited=True
        if pos is None and not just_exited and t in arrivals:
            chosen=None
            for e in arrivals[t]:
                coin=e["inst"]
                b=raw[coin].get(t)
                if not b or b[0]<=0:continue
                if mode=="EARLY":
                    result=m.returns(raw[coin],states[coin],{
                        "inst":coin,"signal_ms":t,"side":e["side"],
                        "day":e["entry_day"]},(.015,.01,12),END)
                    if result is None:continue
                    gain,leaves,why=result
                    if leaves>END:continue
                chosen=e
                break
            if chosen:
                coin=chosen["inst"]
                opens[chosen["entry_day"]]+=1
                pos={"coin":coin,"side":chosen["side"],"start":t,
                     "start_day":chosen["entry_day"],"entry_price":raw[coin][t][0],
                     "ratio":chosen["ratio"],"rank":chosen["rank"],
                     "funded": mode!="EARLY"}
                if mode=="EARLY":
                    gross,end_ms,why=m.returns(raw[coin],states[coin],{
                        "inst":coin,"signal_ms":t,"side":chosen["side"],
                        "day":chosen["entry_day"]},(.015,.01,12),END)
                    pos.update({"exit_plan_ms":end_ms,
                                "exit_plan_gross_pct":gross,
                                "exit_plan_reason":why})
        current=compute_equity(equity,pos,raw,t+FIVE)
        peak=max(peak,current)
        low_equity=min(low_equity,current)
        maxdraw=max(maxdraw,peak-current)
    # Forced close on final candle. Do not count unrealized gain as closed
    # until calculating explicit end-of-test mark-to-market flattening.
    if pos:
        coin=pos["coin"]
        candle=raw[coin].get(END-FIVE)
        if candle is None:raise RuntimeError("Missing last bar close")
        sign=1 if pos["side"]=="LONG" else -1
        gross=100*sign*(candle[3]/pos["entry_price"]-1)
        fund=(max(0,END-pos["start"])//EIGHT_H)*FUNDING_8H_EST if pos["funded"] else 0.
        net=gross-COST-fund
        equity+=NOTIONAL*net/100
        positions.append({"coin":coin,"side":pos["side"],
              "entry_uk":stamp(pos["start"]),"exit_uk":stamp(END),
              "entry_day":pos["start_day"],"exit_day":uk(END).date().isoformat(),
              "minutes_held":(END-pos["start"])/60000,
              "exit":"END_OF_TEST_LIQUIDATION","net_pct":round(net,4),
              "net_usdt":round(NOTIONAL*net/100,5),
              "estimated_funding_pct":round(fund,4),
              "source_volume_multiple":round(pos["ratio"],3),
              "source_rank":pos["rank"]})
        pos=None
        exitcounts["END_OF_TEST_LIQUIDATION"]+=1
    daily[DAYS[-1]]={"equity_end_usdt":equity}
    running=CAPITAL
    per_day=[]
    for d in DAYS:
        end_equity=daily[d]["equity_end_usdt"]
        n=opens.get(d,0)
        pnl=end_equity-running
        per_day.append({"date_uk":d,"entries":n,"equity_end_usdt":round(end_equity,5),
            "pnl_usdt":round(pnl,5),"return_on_starting_25usdt_pct":round(100*pnl/CAPITAL,4),
            "profitable":pnl>0})
        running=end_equity
    ptotal=equity-CAPITAL
    signed=round(100*ptotal/CAPITAL,3)
    positive=sum(d["profitable"] for d in per_day)
    negative=sum(d["pnl_usdt"]<0 for d in per_day)
    return {"strategy":label,"candidate_signals":len(events),
      "total_completed_trade_count":len(positions),
      "profitable_closed_trades":sum(p["net_pct"]>0 for p in positions),
      "losing_or_zero_closed_trades":sum(p["net_pct"]<=0 for p in positions),
      "net_usdt":round(ptotal,5),
      "net_pct_initial_25usdt":signed,
      "positive_days":positive,"negative_days":negative,
      "flat_days":N_DAYS-positive-negative,
      "avg_net_usdt_day":round(ptotal/N_DAYS,5),
      "worst_single_day_pct_of_initial_capital":min(x["return_on_starting_25usdt_pct"] for x in per_day),
      "max_drawdown_5min_close_usdt":round(maxdraw,5),
      "max_drawdown_pct_initial_capital":round(100*maxdraw/CAPITAL,3),
      "min_equity_mark_usdt":round(low_equity,5),
      "exit_counts":dict(exitcounts),
      "daily":per_day,
      "trades":positions}

def main():
    raw=load()
    state,early,flip,cover=prepare(raw)
    print("HISTORICAL_RANKING="+json.dumps({
        "sources":390,"first":stamp(START),"end":stamp(END),
        "rank_snapshots":len(cover),"median_coverage":statistics.median(cover),
        "raw_early":len(early),"raw_flip":len(flip)}),flush=True)
    a=backtest("EARLY_12_TO_18_UK_TP1P5_SL1_60M",early,raw,state,"EARLY")
    b=backtest("FLIP24_NO_TP_NO_SL_VOL2_BODY0P1",flip,raw,state,"FLIP24")
    report={
        "period_uk":[stamp(START),stamp(END)],
        "days":DAYS,
        "universe":"390 currently listed crypto-USDT linear swaps, historical point-in-time TOP200 at each 5m candle, all 8 days",
        "ranking_snapshots":len(cover),"median_rank_population":statistics.median(cover),
        "notional_each_trade_usdt":NOTIONAL,"capital_reference_usdt":CAPITAL,
        "commission_roundtrip_est_pct":0.12,
        "slippage_roundtrip_est_pct":0.10,
        "funding_assumption_adverse_each_8h_pct":FUNDING_8H_EST,
        "exact_entry_rules":{
          "EARLY":"TOP200, 12-18 UK entry only, after closed 5m candle of same color as confirmed volume regime within 0..3 candles following strict peak-volume flip, volume > prior candle and >=4x median last20, body >=0.4%, enter next open. TP1.5 SL1, max 60min, exit on volume side mismatch.",
          "FLIP24":"TOP200, 00-24 UK anytime, after closed 5m candle that STRICTLY flips volume regime (opposite color volume > former same-color max), candle volume >=2x median prior20, body>=0.1%, enter next open; exit after opposite STRICT volume flip at next open or max24h, whichever first; no price TP/SL. UK midnight resets peak and trend definition."},
        "same_time_selection":"lowest historical 24h gain rank, then alphabetical coin, no future returns",
        "position_rule":"one non-overlapping global 20 USDT position, never open on same 5m tick as exit, no leverage modeled, UK daily mark-to-market accounting",
        "EARLY":a,"FLIP24":b,
        "limitations":"Only 8 full UK days with 390 currently-listed eligible swaps; not the original 45 days on 46 instruments. Historical ranking among current survivors rather than true delisted universe. Costs estimate 0.22% roundtrip, plus adverse 0.01% per 8h for FLIP24, not actual historical funding payments. SL/TP within 5m bar assumed conservative SL-first; NO funding history, price spread orderbook, exact fills, or liquidations. Max drawdown only measured using 5m candle closes, ignores within-candle worst case. Profitable 8 days does not prove repeatability. No LIVE changes."
    }
    out=Path("blofin_early_vs_flip24_same_top200_8d.json")
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    def short(result):
        return {k:v for k,v in result.items() if k!="trades"}
    print("STRATEGY_COMPARE_EARLY="+json.dumps(short(a),separators=(",",":")),flush=True)
    print("STRATEGY_COMPARE_FLIP24="+json.dumps(short(b),separators=(",",":")),flush=True)
    print("FINAL_VERDICT="+json.dumps({
        "better_total_profit":"FLIP24" if b["net_usdt"]>a["net_usdt"] else "EARLY",
        "better_profitable_days":"FLIP24" if b["positive_days"]>a["positive_days"] else "EARLY",
        "less_drawdown":"FLIP24" if b["max_drawdown_5min_close_usdt"]<a["max_drawdown_5min_close_usdt"] else "EARLY",
        "early_total_usdt":a["net_usdt"],"flip24_total_usdt":b["net_usdt"],
        "early_profitable_days":a["positive_days"],"flip24_profitable_days":b["positive_days"],
        "early_dd_usdt":a["max_drawdown_5min_close_usdt"],
        "flip24_dd_usdt":b["max_drawdown_5min_close_usdt"]},separators=(",",":")),flush=True)

if __name__=="__main__":main()
