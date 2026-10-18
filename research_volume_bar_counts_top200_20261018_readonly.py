"""READ ONLY: count 5m green bars before a larger red volume, and vice versa.

Market: existing complete public 390-coin archive 2026-10-17 18:00 to
2026-10-18 18:00 UK. TOP200 computed point-in-time at each closed 5m candle
from rolling 24h returns; no private API, no live orders, no broker state.

LIVE strategy at 2026-10-18:
 - LONG entry green after red, green volume > red, both <= own prior 20 avg
 - SHORT entry red after green, red volume > green, both <= own prior 20 avg
 - Close after first opposite *completed* 5m candle volume > initial
   signal candle volume, no mean filter on exits.
Per-coin sequential replay, no 4 concurrent position or capital constraints.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
import json, math, statistics
from pathlib import Path
from zoneinfo import ZoneInfo

import short60_volume_mean10_24hour_readonly as frozen

F=300_000
UK=ZoneInfo("Europe/London")
START=frozen.START
END=frozen.END

def timestamp(ts):
    return datetime.fromtimestamp(ts/1000, UK).strftime("%Y-%m-%d %H:%M")

def color(bar):
    if bar[3] > bar[0]:return "GREEN"
    if bar[3] < bar[0]:return "RED"
    return None

def mean20(rows, ts):
    prev=[rows.get(ts-i*F) for i in range(1,21)]
    if any(x is None for x in prev):return None
    out=sum(x[4] for x in prev)/20
    return out if out>0 and math.isfinite(out) else None

def qualify_entry(rows, ts, use_ma20):
    now=rows.get(ts)
    prev=rows.get(ts-F)
    if not now or not prev:return None
    col=color(now)
    old=color(prev)
    if not col or not old or col==old or now[4]<=prev[4] or now[4]<=0:
        return None
    if use_ma20:
        m0,m1=mean20(rows,ts),mean20(rows,ts-F)
        if m0 is None or m1 is None or now[4]>m0 or prev[4]>m1:
            return None
    return col

def stats_int(nums):
    if not nums:return {"n":0}
    ordered=sorted(nums)
    def pct(p):return ordered[min(len(ordered)-1,max(0,math.ceil(p*len(ordered))-1))]
    return {"n":len(nums),"mean":round(statistics.mean(nums),2),
            "median":statistics.median(nums),"p25":pct(.25),"p75":pct(.75),
            "p90":pct(.90),"min":min(nums),"max":max(nums)}

def replay(market,ranks,use_ma20):
    closed={"LONG":[],"SHORT":[]}
    opened=Counter()
    censored=Counter()
    raw_wins=Counter()
    for inst,rows in market.items():
        pos=None
        for ts in range(START,END,F):
            b=rows.get(ts)
            if b is None:continue
            col=color(b)
            if pos:
                if col==pos["colour"]:
                    pos["same_after_entry"]+=1
                    if pos["opp_before"]==0:pos["consecutive_same_after_entry"]+=1
                elif col is not None:
                    if b[4]>pos["entry_vol"]:
                        side=pos["side"]
                        t=ts-pos["ts"]
                        closed[side].append({
                            "inst":inst,"entry_ts":pos["ts"],"exit_ts":ts,
                            "entry_volume":pos["entry_vol"],"exit_volume":b[4],
                            "same_including_entry":pos["same_after_entry"]+1,
                            "same_after_entry":pos["same_after_entry"],
                            "consecutive_same_including_entry":pos["consecutive_same_after_entry"]+1,
                            "opposite_small_before_exit":pos["opp_before"],
                            "bars_until_larger_opposite":t//F,
                            "minutes_until_larger_opposite":t//60000,
                        })
                        raw_wins[side]+=1
                        pos=None
                        continue
                    pos["opp_before"]+=1
                # Never generate overlapping entries on an already active coin.
                continue
            if inst not in ranks.get(ts,{}):continue
            signal=qualify_entry(rows,ts,use_ma20)
            if not signal:continue
            side="LONG" if signal=="GREEN" else "SHORT"
            opened[side]+=1
            pos={"side":side,"colour":signal,"ts":ts,
                 "entry_vol":b[4],"same_after_entry":0,
                 "consecutive_same_after_entry":0,"opp_before":0}
        if pos:
            censored[pos["side"]]+=1

    results={"use_MA20_on_entry":use_ma20,"opened":dict(opened),
             "closed":dict(raw_wins),"censored_not_closed_before_end":dict(censored),
             "directions":{}}
    for side in ("LONG","SHORT"):
        values=closed[side]
        results["directions"][side]={
            "larger_opposite_color":"RED" if side=="LONG" else "GREEN",
            "same_color":"GREEN" if side=="LONG" else "RED",
            "closed_count":len(values),
            "same_color_bars_INCLUDING_entry":stats_int([v["same_including_entry"] for v in values]),
            "same_color_bars_AFTER_entry":stats_int([v["same_after_entry"] for v in values]),
            "consecutive_same_color_INCLUDING_entry":stats_int([v["consecutive_same_including_entry"] for v in values]),
            "all_5m_bars_until_opposite":stats_int([v["bars_until_larger_opposite"] for v in values]),
            "opposite_color_smaller_candles_before_exit":stats_int([v["opposite_small_before_exit"] for v in values]),
            "distribution_same_color_including_entry":{
                str(k):n for k,n in sorted(Counter(min(v["same_including_entry"],10) for v in values).items())
            },
            "distribution_bars_until_exit":{
                str(k):n for k,n in sorted(Counter(min(v["bars_until_larger_opposite"],10) for v in values).items())
            },
            "examples":values[:2],
        }
    return results

def main():
    market,errors,total=frozen.load()
    if len(market)<300 or len(errors)>10:raise RuntimeError("Insufficient archived coverage")
    rankings,coverage=frozen.rankings(market)
    if len(rankings)!=289:raise RuntimeError(f"Expected 289 TOP200 ranks including warmup; got {len(rankings)}")
    report={"period_UK":f"{timestamp(START)} to {timestamp(END)}",
            "timeframe_minutes":5,"universe_archive_symbols":total,
            "market_symbols_with_data":len(market),
            "median_rank_candidates_available":statistics.median(coverage),
            "signals_not_actual_broker_trades":True,
            "exit_greater_than_entry_signal_candle_only":True,
            "MA20_entry_only":replay(market,rankings,True),
            "no_average_filter_entry_comparison":replay(market,rankings,False)}
    Path("volume_colour_to_larger_opposite_top200_24h_result.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("VOLUME_BAR_COUNTS_RESULT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)

if __name__=="__main__":main()
