"""READ ONLY: hunt for ONE repeating volume setup with >=3 profitable daily entries.

POINT IN TIME: all currently listed BloFin crypto USDT linear swaps, rank ALL
available symbols each closed 5m bar by rolling 24h price change (matching
LIVE's 24h gainers concept). Keep top 200 for entry signals at THAT timestamp.
Signal only after 5m bar closes; enter next bar open; no overlapping positions.
Reset volume maximum at each UK local midnight; force close positions by
23:59/UK midnight for independently auditable daily PnL. No private API used.
Look for >=3 filled trades and >=0.6 USDT NET per day on 20 USDT position.
This is ambitious; if none achieves, report failure, not imaginary profit.

Training: four complete UK days; validation: two following; holdout: final two.
Only accept a strategy when fixed rules pass all three without later retuning.
Fees 0.12% roundtrip plus adverse 0.05% each side, no funding estimate.
Uses all eligible symbols with public history, not old bot trade-history universe.
"""
from __future__ import annotations
import concurrent.futures
import collections
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import itertools
import json
import math
import statistics
import threading
import time
from pathlib import Path

import requests

BASE="https://openapi.blofin.com"
FIVE=300_000
UK=ZoneInfo("Europe/London")
THREADS=12
MIN_CALL_GAP=0.085
FEE_PCT=0.12
SLIPPAGE_PCT=0.10  # 0.05% on both entry and exit, conservative
COST_PCT=FEE_PCT+SLIPPAGE_PCT
NOTIONAL=20.0
GOAL_USDT=0.03*NOTIONAL
NUM_DAYS=8
WARMUP_BARS=300
MAX_PAGES=17
MIN_RANK_COVERAGE=200
FAMILY=("FLIP","FOLLOW","PULLBACK","BREAKOUT6","EARLY")
VOL_MULT=(1.0,1.7,2.5)
BODY_MIN=(0.10,0.25,0.40)
SIDES=("BOTH","LONG","SHORT")
TP_LIST=(0.01,0.015,0.02)
SL_LIST=(0.005,0.01)
HOLD_LIST=(3,6,12)  # minutes = 15/30/60
EXIT_LIST=tuple(itertools.product(TP_LIST,SL_LIST,HOLD_LIST))
PACING=threading.Lock()
LAST_CALL=[0.0]
LOCAL=threading.local()


def get_data(endpoint,params=None):
    for n in range(5):
        try:
            with PACING:
                delta=time.monotonic()-LAST_CALL[0]
                if delta<MIN_CALL_GAP:
                    time.sleep(MIN_CALL_GAP-delta)
                LAST_CALL[0]=time.monotonic()
            sess=getattr(LOCAL,"session",None)
            if sess is None:
                sess=LOCAL.session=requests.Session()
            response=sess.get(BASE+endpoint,params=params,timeout=20)
            if response.status_code==429 or response.status_code>=500:
                time.sleep(1.5*(n+1))
                continue
            response.raise_for_status()
            obj=response.json()
            if str(obj.get("code"))!="0":
                raise RuntimeError("API nonzero")
            return obj.get("data") or []
        except (requests.RequestException,ValueError,RuntimeError) as exc:
            if n==4:
                raise RuntimeError(type(exc).__name__)
            time.sleep(.7*(n+1))
    raise RuntimeError("Failed API call")


def list_swaps():
    result=[]
    for row in get_data("/api/v1/market/instruments"):
        if (row.get("state")=="live"
            and row.get("instType")=="SWAP"
            and row.get("contractType")=="linear"
            and row.get("settleCurrency")=="USDT"
            and str(row.get("assetClass") or "").lower()=="crypto"):
            inst=str(row.get("instId") or "")
            if inst: result.append(inst)
    return sorted(set(result))


def candles(inst,lastclose,oldest_required):
    allrows={}
    cursor=lastclose
    count=0
    for _ in range(MAX_PAGES):
        rows=get_data("/api/v1/market/candles",
                      {"instId":inst,"bar":"5m","after":str(cursor),"limit":"240"})
        count+=1
        times=[]
        for x in rows:
            try:
                if len(x)<9 or str(x[8])!="1":continue
                t=int(x[0])
                if t>=lastclose or t<oldest_required-3600_000:
                    continue
                o,h,l,c,v=(float(x[i]) for i in (1,2,3,4,5))
                if o<=0 or h<=0 or l<=0 or c<=0 or v<0:
                    continue
                allrows[t]=(o,h,l,c,v)
                times.append(t)
            except (ValueError,IndexError,TypeError):continue
        if not rows:
            break
        oldest=min(int(x[0]) for x in rows if x and x[0])
        if oldest <= oldest_required or oldest>=cursor:
            break
        cursor=oldest
    return allrows,count


def volume_states(data,timekeys):
    """Daily peak reset at UK midnight; record only closed candles."""
    states={}
    direction=None
    peak=0.0
    day=None
    for ts in timekeys:
        b=data.get(ts)
        if b is None:continue
        clockdate=datetime.fromtimestamp(ts/1000,UK).date()
        if clockdate!=day:
            day=clockdate
            direction=None
            peak=0.0
        color="LONG" if b[3]>b[0] else "SHORT" if b[3]<b[0] else None
        flip=False
        if color and b[4]>0:
            if direction is None:
                direction=color;peak=b[4]
            elif color==direction:
                peak=max(peak,b[4])
            elif b[4]>peak:
                direction=color
                peak=b[4]
                flip=True
        states[ts]=(direction,flip,peak)
    return states


def calc_event(inst,ts,data,states,rank):
    # ts is a 5m candle open; signal known at ts+5m
    b=data.get(ts)
    if b is None:return []
    o,h,l,c,v=b
    if o<=0 or v<=0:return []
    side,flip,peak=states.get(ts,(None,False,0))
    if side is None:return []
    color="LONG" if c>o else "SHORT" if c<o else None
    vols=[data.get(ts-j*FIVE) for j in range(1,21)]
    if any(x is None for x in vols):return []
    median=statistics.median(x[4] for x in vols)
    if median<=0:return []
    ratio=v/median
    if ratio<1.0:return []
    body=abs(c/o-1)*100
    if body<0.1:return []
    prev=data.get(ts-FIVE)
    if prev is None:return []
    prevside="LONG" if prev[3]>prev[0] else "SHORT" if prev[3]<prev[0] else None
    families=[]
    if flip and side==color:families.append("FLIP")
    if side==color and v>prev[4]:families.append("FOLLOW")
    if side==color and prevside!=side and v>prev[4]:families.append("PULLBACK")
    recent=[data.get(ts-j*FIVE) for j in range(1,7)]
    if all(recent) and color==side:
        if (side=="LONG" and c>max(x[1] for x in recent)) or (side=="SHORT" and c<min(x[2] for x in recent)):
            families.append("BREAKOUT6")
    # EARLY is 1-3 bars after volume flip; causal check.
    if color==side and v>prev[4] and any(states.get(ts-j*FIVE,(None,False,0))[1] for j in (0,1,2,3)):
        families.append("EARLY")
    if not families:return []
    daykey=datetime.fromtimestamp(ts/1000,UK).strftime("%Y-%m-%d")
    return [{"inst":inst,"signal_ms":ts+FIVE,"sig_ts":ts,"side":side,
             "rank":rank,"ratio":ratio,"body":body,"day":daykey,
             "family":family} for family in families]


def returns(data,states,ev,cfg,analysis_end):
    tp,sl,hold=cfg
    entry_time=ev["signal_ms"]
    entry_bar=data.get(entry_time)
    if entry_bar is None:return None
    price=entry_bar[0]
    if price<=0:return None
    sign=1 if ev["side"]=="LONG" else -1
    original_day=ev["day"]
    for j in range(hold):
        ts=entry_time+j*FIVE
        b=data.get(ts)
        if b is None:return None
        # At UK midnight, we do not carry open positions into the next day.
        if datetime.fromtimestamp(ts/1000,UK).strftime("%Y-%m-%d")!=original_day:
            return None
        if sign==1:
            tp_hit=b[1]>=price*(1+tp)
            sl_hit=b[2]<=price*(1-sl)
        else:
            tp_hit=b[2]<=price*(1-tp)
            sl_hit=b[1]>=price*(1+sl)
        if sl_hit:
            return (-100*sl,ts+FIVE,"SL" if not tp_hit else "AMBIGUOUS_SL")
        if tp_hit:
            return (100*tp,ts+FIVE,"TP")
        if ts+FIVE>=analysis_end:
            return None
        nextday=datetime.fromtimestamp((ts+FIVE)/1000,UK).strftime("%Y-%m-%d")
        if nextday!=original_day:
            pct=100*sign*(b[3]/price-1)
            return (pct,ts+FIVE,"DAY_END")
        if states.get(ts,(None,False,0))[0]!=ev["side"]:
            next_b=data.get(ts+FIVE)
            if next_b is None:return None
            pct=100*sign*(next_b[0]/price-1)
            return (pct,ts+FIVE,"REGIME_FLIP")
    last=entry_time+(hold-1)*FIVE
    px=data[last][3]
    return (100*sign*(px/price-1),last+FIVE,"TIME")


def compute_daily(candidates,outcomes,rule,exitid,days):
    # One 20 USDT global position; last completed trade may not cross day.
    family,mult,body,side=rule
    events=[e for e in candidates.get(family,[])
            if e["ratio"]>=mult and e["body"]>=body
            and (side=="BOTH" or e["side"]==side)]
    events.sort(key=lambda e:(e["signal_ms"],e["inst"]))
    # Do not use signals from future days in selection (independent evaluation).
    byday={day:[] for day in days}
    busy=0
    for e in events:
        if e["day"] not in byday or e["signal_ms"]<busy:continue
        value=outcomes.get((e["inst"],e["signal_ms"],e["side"],exitid))
        if value is None:continue
        pnl,last,why=value
        busy=last
        byday[e["day"]].append((pnl,why,e["inst"]))
    output={}
    for day,entries in byday.items():
        n=len(entries)
        grosspct=sum(e[0] for e in entries)
        netpct=grosspct-COST_PCT*n
        output[day]={"trades":n,"wins_gross":sum(e[0]>0 for e in entries),
                     "wins_net":sum(e[0]>COST_PCT for e in entries),
                     "profit_usdt":round(netpct*NOTIONAL/100,4),
                     "return_on_20usdt_pct":round(netpct,3)}
    return output


def assess(per_day):
    ys=list(per_day.values())
    if not ys:return {}
    hits=sum(x["profit_usdt"]>=GOAL_USDT for x in ys)
    n3=sum(x["trades"]>=3 for x in ys)
    return {"days":len(ys),"days_at_least_3_trades":n3,
            "days_at_least_3pct":hits,
            "min_trades_in_day":min(x["trades"] for x in ys),
            "min_day_net_usdt":min(x["profit_usdt"] for x in ys),
            "total_net_usdt":round(sum(x["profit_usdt"] for x in ys),4),
            "avg_net_usdt_per_day":round(sum(x["profit_usdt"] for x in ys)/len(ys),4),
            "median_net_usdt_per_day":round(statistics.median(x["profit_usdt"] for x in ys),4),
            "daily":per_day}


def main():
    current=datetime.now(UK)
    # last 8 COMPLETED UK days (avoid partial forward-looking day)
    final_midnight=current.replace(hour=0,minute=0,second=0,microsecond=0)
    first_midnight=final_midnight-timedelta(days=NUM_DAYS)
    first=int(first_midnight.astimezone(timezone.utc).timestamp()*1000)
    end=int(final_midnight.astimezone(timezone.utc).timestamp()*1000)
    times=list(range(first-WARMUP_BARS*FIVE,end,FIVE))
    print(f"PERIOD={first_midnight.isoformat()} TO={final_midnight.isoformat()} PREP_CANDLES={len(times)}",flush=True)
    symbols=list_swaps()
    print(f"ELIGIBLE_USDT_SWAP_INSTRUMENTS={len(symbols)}",flush=True)
    if len(symbols)<200:raise RuntimeError("Fewer than 200 eligible swaps")
    raw={}
    fail={}
    t0=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        jobs={pool.submit(candles,inst,end,first-WARMUP_BARS*FIVE):inst for inst in symbols}
        for n,job in enumerate(concurrent.futures.as_completed(jobs),1):
            inst=jobs[job]
            try:
                rows,pages=job.result()
                if len(rows)>100:
                    raw[inst]=rows
                else:
                    fail[inst]=f"Insufficient candles: {len(rows)}"
            except Exception as ex:fail[inst]=str(ex)
            if n%75==0:
                print(f"FETCH_PROGRESS={n}/{len(symbols)} GOOD={len(raw)} FAILED={len(fail)} ELAPSED={time.monotonic()-t0:.1f}",flush=True)
    print(f"FETCH_FINISHED={len(raw)}/{len(symbols)} FAILURES={len(fail)} SECONDS={time.monotonic()-t0:.1f}",flush=True)
    if len(raw)<200:raise RuntimeError("Fewer than 200 instruments with candle history")
    # E.g. 2026-10-09...2026-10-17, never use future for ranking.
    states={inst:volume_states(bars,times) for inst,bars in raw.items()}
    byfam=collections.defaultdict(list)
    rankcounts=[]
    rank_errors=[]
    for i,ts in enumerate(times):
        if ts<first:continue
        ranks=[]
        prior=ts-288*FIVE
        for inst,bars in raw.items():
            cur=bars.get(ts)
            old=bars.get(prior)
            if cur and old and old[3]>0:
                ranks.append((cur[3]/old[3]-1,inst))
        if len(ranks)<MIN_RANK_COVERAGE:
            rank_errors.append((ts,len(ranks)))
            continue
        ranks.sort(reverse=True)
        selected=ranks[:200]
        rankcounts.append(len(ranks))
        for rank,(_,inst) in enumerate(selected,1):
            entries=calc_event(inst,ts,raw[inst],states[inst],rank)
            for e in entries:byfam[e["family"]].append(e)
    print(f"RANKING_SNAPSHOTS={len(rankcounts)} MEDIAN_COVERAGE={statistics.median(rankcounts) if rankcounts else 0} INCOMPLETE={len(rank_errors)}",flush=True)
    print("SETUP_EVENTS="+json.dumps({k:len(v) for k,v in byfam.items()}),flush=True)
    if len(rankcounts)<NUM_DAYS*200:
        raise RuntimeError("Not enough reliable 5m point-in-time ranking snapshots")
    all_events={ (e["inst"],e["signal_ms"],e["side"]):e for entries in byfam.values() for e in entries}
    outcome_cache={}
    for k,e in all_events.items():
        for index,cfg in enumerate(EXIT_LIST):
            v=returns(raw[e["inst"]],states[e["inst"]],e,cfg,end)
            if v is not None:outcome_cache[(*k,index)]=v
    print(f"OUTCOMES_CACHED={len(outcome_cache)} DISTINCT_ENTRY_CANDIDATES={len(all_events)}",flush=True)

    days=[(first_midnight+timedelta(days=i)).date().isoformat() for i in range(NUM_DAYS)]
    trdays=days[:4]
    vdays=days[4:6]
    hdays=days[6:8]
    candidates=[]
    scanned=0
    training_best=[]
    for rule in itertools.product(FAMILY,VOL_MULT,BODY_MIN,SIDES):
        for exitid,cfg in enumerate(EXIT_LIST):
            scanned+=1
            training=assess(compute_daily(byfam,outcome_cache,rule,exitid,trdays))
            # Minimum 3 FILLED global trades on all four train days, and >=+3% avg train
            if training["days_at_least_3_trades"]<4:continue
            if training["avg_net_usdt_per_day"]<GOAL_USDT:continue
            training_best.append((rule,exitid,training))
    # rank candidates by success over training, then evaluate validation. Never select using holdout.
    training_best.sort(key=lambda x:(x[2]["days_at_least_3pct"],x[2]["min_day_net_usdt"],x[2]["avg_net_usdt_per_day"]),reverse=True)
    for rule,idx,tr in training_best:
        valid=assess(compute_daily(byfam,outcome_cache,rule,idx,vdays))
        if valid["days_at_least_3_trades"]<2 or valid["avg_net_usdt_per_day"]<GOAL_USDT:
            continue
        candidates.append((rule,idx,tr,valid))
    candidates.sort(key=lambda x:(x[2]["days_at_least_3pct"]+x[3]["days_at_least_3pct"],
                                  x[3]["min_day_net_usdt"],x[2]["avg_net_usdt_per_day"]+x[3]["avg_net_usdt_per_day"]),reverse=True)
    top=[]
    for rule,idx,tr,v in candidates[:5]:
        hold=assess(compute_daily(byfam,outcome_cache,rule,idx,hdays))
        desc={"entry_family":rule[0],"min_vol_vs_median20":rule[1],
              "min_body_pct":rule[2],"direction":rule[3],
              "tp_pct":EXIT_LIST[idx][0]*100,"sl_pct":EXIT_LIST[idx][1]*100,
              "max_minutes":EXIT_LIST[idx][2]*5,
              "training":tr,"validation":v,"untouched_holdout":hold,
              "passes_every_holdout_day":hold["days_at_least_3pct"]==2 and hold["days_at_least_3_trades"]==2}
        top.append(desc)
    # Whether model did not find 3%, report best training-only setup as diagnostic,
    # without selecting holdout or claiming confirmed validation success.
    fallbacks=[]
    if not candidates:
        for rule,idx,tr in training_best[:3]:
            v=assess(compute_daily(byfam,outcome_cache,rule,idx,vdays))
            fallbacks.append({"family":rule[0],"volume_min":rule[1],"body_pct_min":rule[2],
                              "side":rule[3],"tp_pct":EXIT_LIST[idx][0]*100,
                              "sl_pct":EXIT_LIST[idx][1]*100,
                              "max_hold_minutes":EXIT_LIST[idx][2]*5,
                              "training":tr,"validation":v})
    report={
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "period_uk":{"first_date":days[0],"last_date":days[-1],"all_eight_days":days},
        "instruments_currently_eligible":len(symbols),
        "instruments_with_market_candles":len(raw),
        "missing_instruments":fail,
        "ranking_definition":"Point-in-time TOP200 of all current live eligible crypto USDT linear swaps by 5m-closed rolling 24h PRICE change. Not direct historical exchange ticker open24h; current-listing survivorship bias.",
        "ranking_snapshots":len(rankcounts),
        "ranking_median_available_instruments":statistics.median(rankcounts) if rankcounts else None,
        "incomplete_ranking_snapshots":len(rank_errors),
        "events_by_family":{name:len(byfam.get(name,[])) for name in FAMILY},
        "scenarios_scanned":scanned,
        "training_meets_three_trades_daily_and_3pct_mean":len(training_best),
        "passes_training_and_validation_3pct_mean":len(candidates),
        "selected_before_holdout":top,
        "train_only_diagnostics_if_no_validated":fallbacks,
        "primary_objective":"ONE identical setup, >=3 actual non-overlapping filled positions every day AND >=+3% NET return on a fixed 20 USDT/day reference, after estimated costs; 4 days training, 2 validation, 2 untouched holdout.",
        "uk_day_reset":True,
        "cost_roundtrip_pct":COST_PCT,
        "commission_pct":FEE_PCT,
        "slippage_pct":SLIPPAGE_PCT,
        "position_notional_usdt":NOTIONAL,
        "limitations":"Historical market ranks are reconstructed among instruments currently listed; currently delisted symbols absent; rolling 24h close change not exact ticker open24h; funding and liquidation ignored, spread captured only by assumed adverse slip; equal-time signals resolved deterministically by coin name, not orderbook; OHLC TP/SL same 5m candle conservative SL, unable to know intrabar path; multiple-testing/short 8d. Full 3% everyday not guaranteed. Did NOT touch LIVE."
    }
    Path("blofin_top200_repeat_setup_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("TOP200_REPEATING_SETUP="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    main()
