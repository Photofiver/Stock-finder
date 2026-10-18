"""READ ONLY: exact rolling 24h count of frozen SHORT after 4 green 5m bars.

Cutoff is fixed 2026-10-18 01:20 Europe/London, immediately after user's
question, so window is 2026-10-17 01:20 through 2026-10-18 01:20 UK.
Matches previously discovered EXHAUST3_FADE variant:
previous 3 5m bars green, signal 5m bar green, signal bar
volume >= 2.5x median volume of PREVIOUS 20 bars,
signal body >= 0.5%, symbol TOP200 by causal rolling 24h % price gain.
Each (coin, 5m bar) counts ONE raw signal. No trade is placed.
"""
from __future__ import annotations
import collections
import concurrent.futures
from datetime import datetime
import gzip
import json
from pathlib import Path
import statistics
import sys
import time
from zoneinfo import ZoneInfo

import blofin_top200_repeat_setup_research as m

UK=ZoneInfo("Europe/London")
STEP=300_000
ASOF="2026-10-18T01:20:00+01:00"
CUTOFF=int(datetime.fromisoformat(ASOF).timestamp()*1000)
START=CUTOFF-24*60*60*1000
MIDNIGHT=int(datetime.fromisoformat("2026-10-18T00:00:00+01:00").timestamp()*1000)
NUM_SHARDS=4
OUTPUT="blofin_top200_four_green_exact_rolling24h.json"


def fetch(n):
    names=m.list_swaps()
    targets=names[n::NUM_SHARDS]
    if len(names)<200:
        raise RuntimeError("Fewer than TOP200 available at BloFin")
    m.MIN_CALL_GAP=.20
    fresh={}
    failed={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        jobs={pool.submit(m.candles,inst,CUTOFF,MIDNIGHT):inst for inst in targets}
        for ix,f in enumerate(concurrent.futures.as_completed(jobs),1):
            inst=jobs[f]
            try:
                rows,pages=f.result()
                # Complete closed candles including the Oct 18 rolling 24h extension
                relevant={str(ts):list(row) for ts,row in rows.items()
                          if MIDNIGHT<=ts and ts+STEP<=CUTOFF}
                if relevant:
                    fresh[inst]=relevant
                else:
                    failed[inst]=f"No completed candles since UK midnight (retrieved {len(rows)})"
            except Exception as exc:
                failed[inst]=repr(exc)
            if ix%20==0:
                print(f"FRESH_SHARD_PROGRESS={n}:{ix}/{len(targets)} success={len(fresh)} failed={len(failed)}",flush=True)
    path=f"rolling24_shard_{n}.json.gz"
    with gzip.open(path,"wt",encoding="utf-8",compresslevel=4) as f:
        json.dump({"asof":ASOF,"shard":n,"eligible":len(names),
                   "instrument_names":targets,"rows":fresh,"failures":failed},f,separators=(",",":"))
    print(f"FRESH_SHARD_DONE={n} GOOD={len(fresh)} FAIL={len(failed)}",flush=True)


def analyze():
    historical={}
    errors={}
    listed=[]
    for n in range(NUM_SHARDS):
        path=Path("historical")/f"shard_{n}.json.gz"
        with gzip.open(path,"rt",encoding="utf-8") as f:
            d=json.load(f)
        if d["asof_uk"]!="2026-10-18":
            raise RuntimeError("Wrong historical source")
        listed.extend(d["symbols_in_shard"])
        errors.update(d["errors"])
        for inst,rows in d["candle_rows"].items():
            historical[inst]={int(t):tuple(map(float,bar)) for t,bar in rows.items()}
    allfresh={}
    new_failed={}
    for n in range(NUM_SHARDS):
        path=Path("fresh")/f"rolling24_shard_{n}.json.gz"
        with gzip.open(path,"rt",encoding="utf-8") as f:
            d=json.load(f)
        if d["asof"]!=ASOF or d["shard"]!=n:
            raise RuntimeError("Wrong freshly fetched data or shard")
        new_failed.update(d["failures"])
        for inst,rows in d["rows"].items():
            allfresh[inst]={int(t):tuple(map(float,bar)) for t,bar in rows.items()}
    for inst,rows in allfresh.items():
        historical.setdefault(inst,{}).update(rows)
    print(f"MARKET_COVERAGE=HISTORICAL={len(historical)} FRESH={len(allfresh)} FAILED={len(new_failed)}",flush=True)
    if len(historical)<200 or len(allfresh)<300:
        raise RuntimeError("Too little market history for TOP200 validation")

    summary=[]
    for ts in range(START,CUTOFF,STEP):
        ranking=[]
        oldts=ts-288*STEP
        for inst,bars in historical.items():
            c=bars.get(ts)
            p=bars.get(oldts)
            if c is not None and p is not None and p[3]>0:
                ranking.append((c[3]/p[3]-1,inst))
        if len(ranking)<200:
            raise RuntimeError("No reliable TOP200 for candle "+str(ts)+" eligible="+str(len(ranking)))
        ranking.sort(reverse=True)
        selected=ranking[:200]
        for rank,(_,inst) in enumerate(selected,1):
            bars=historical[inst]
            b=bars.get(ts)
            if b is None:continue
            o,h,l,c,vol=b
            if o<=0 or c<=o or vol<=0:continue
            prev3=[bars.get(ts-i*STEP) for i in (1,2,3)]
            if any(x is None or x[3]<=x[0] for x in prev3):continue
            if 100*(c/o-1)<.5:continue
            vol20=[bars.get(ts-i*STEP) for i in range(1,21)]
            if any(x is None for x in vol20):continue
            median=statistics.median(x[4] for x in vol20)
            if median<=0:continue
            ratio=vol/median
            if ratio<2.5:continue
            entry_ts=ts+STEP
            summary.append({"inst":inst,"rank":rank,
                "signal_close_ms":entry_ts,
                "volume_ratio":round(ratio,2),
                "body_pct":round(100*(c/o-1),3),
                "entry_uk":datetime.fromtimestamp(entry_ts/1000,UK).isoformat(),
            })

    slot_counts=collections.Counter()
    coins=collections.Counter()
    times=collections.Counter()
    by_day=collections.Counter()
    for row in summary:
        t=row["signal_close_ms"]
        timevalue=datetime.fromtimestamp(t/1000,UK)
        slot_counts[f"{timevalue.hour//8*8:02d}-{(timevalue.hour//8+1)*8:02d}"]+=1
        by_day[str(timevalue.date())]+=1
        coins[row["inst"]]+=1
        times[t]+=1
    report={
        "cutoff_uk":ASOF,
        "window_start_uk":datetime.fromtimestamp(START/1000,UK).isoformat(),
        "window_end_uk":ASOF,
        "full_24h":True,
        "total_matching_signal_events":len(summary),
        "distinct_coins":len(coins),
        "distinct_5m_signal_times":len(times),
        "by_uk_day":dict(by_day),
        "by_8hour_session":dict(slot_counts),
        "historical_market_count":len(historical),
        "fresh_market_count":len(allfresh),
        "missing_fresh_markets":new_failed,
        "eligible_390_listed_as_of_dataset":len(set(listed)),
        "first_5_sample_signals":summary[:5],
        "last_5_sample_signals":summary[-5:],
        "definition":"SHORT after 4 consecutive green 5m closed candles; last candle volume >=2.5 times previous20 median, body >=0.5%; in TOP200 ranked by rolling 24h close change among currently listed USDT perpetual coins.",
        "limitations":"Raw signals can overlap on multiple coins at same instant and are NOT 3 actual daily executions or winning trades; reconstructed TOP200 among currently listed instruments (survivorship bias), missing fresh symbol candles may change rank. Fee/slippage irrelevant to raw signal count. No live trade keys or changes."
    }
    Path(OUTPUT).write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("ROLLING_24H_SETUP_COUNT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    if sys.argv[1]=="fetch":
        fetch(int(sys.argv[2]))
    elif sys.argv[1]=="analyze":
        analyze()
    else:
        raise SystemExit("Use fetch <0..3> or analyze")
