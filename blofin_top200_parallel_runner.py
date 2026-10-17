"""Parallel read-only fetch and 8-day frozen TOP200 repeated-setup research.

Stage fetch: 1/4 of current live BloFin crypto USDT linear instruments;
each worker slows itself to respect market public API. Saves gzipped actual
closed 5m OHLCV in GitHub artifact only, no source/live state edits.
Stage analyze: downloads all shards, verifies coverage, feeds historical
candles to unchanged main research algorithm, preserving the same ASOF UK day.
"""
import concurrent.futures
from datetime import datetime
import gzip
import json
import os
import sys
import time
from pathlib import Path

import blofin_top200_repeat_setup_research as original

ASOF=os.getenv("RESEARCH_ASOF_UK","2026-10-17")
NUM_SHARDS=4


def anchor():
    return datetime.fromisoformat(ASOF+"T12:00:00").replace(tzinfo=original.UK)


def utc_boundaries():
    final=anchor().replace(hour=0,minute=0,second=0,microsecond=0)
    start=final-original.timedelta(days=original.NUM_DAYS)
    return int(start.astimezone(original.timezone.utc).timestamp()*1000),int(final.astimezone(original.timezone.utc).timestamp()*1000)


def fetch_shard(num):
    allsymbols=original.list_swaps()
    if len(allsymbols)<200:raise RuntimeError("Below 200 live eligible symbols")
    insts=allsymbols[num::NUM_SHARDS]
    start,end=utc_boundaries()
    original.THREADS=5
    original.MIN_CALL_GAP=0.20
    print(f"FETCH_SHARD={num} ALL_ELIGIBLE={len(allsymbols)} TARGET={len(insts)} START_MS={start} END_MS={end}",flush=True)
    raw={}
    err={}
    start_clock=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=original.THREADS) as pool:
        fs={pool.submit(original.candles,inst,end,start-original.WARMUP_BARS*original.FIVE):inst for inst in insts}
        for i,f in enumerate(concurrent.futures.as_completed(fs),1):
            inst=fs[f]
            try:
                candles,pages=f.result()
                if len(candles)>100:raw[inst]={str(k):list(v) for k,v in candles.items()}
                else:err[inst]="Too few bars "+str(len(candles))
            except Exception as e:err[inst]=str(e)
            if i%20==0:print(f"FETCH_SHARD_PROGRESS={num}:{i}/{len(insts)} GOOD={len(raw)} FAIL={len(err)} ELAPSED_SEC={time.monotonic()-start_clock:.1f}",flush=True)
    save={"asof_uk":ASOF,"all_symbol_count":len(allsymbols),
          "symbols_in_shard":insts,"candle_rows":raw,"errors":err}
    filename=f"shard_{num}.json.gz"
    with gzip.open(filename,"wt",encoding="utf-8",compresslevel=4) as out:
        json.dump(save,out,separators=(",",":"))
    print(f"FETCH_SHARD_DONE={num} GOOD={len(raw)} FAIL={len(err)} FILE_BYTES={Path(filename).stat().st_size}",flush=True)


def analyze():
    allrows={}
    errors={}
    allsymbols=[]
    meta=[]
    for shard in range(NUM_SHARDS):
        filename=Path("shards")/f"shard_{shard}.json.gz"
        if not filename.exists():
            matches=list(Path(".").rglob(f"shard_{shard}.json.gz"))
            if len(matches)!=1:raise RuntimeError(f"Missing shard{shard}, matches={len(matches)}")
            filename=matches[0]
        with gzip.open(filename,"rt",encoding="utf-8") as f:payload=json.load(f)
        if payload["asof_uk"]!=ASOF:raise RuntimeError("Mixed dates across shard files")
        meta.append((shard,payload["all_symbol_count"],len(payload["symbols_in_shard"]),len(payload["candle_rows"])))
        allsymbols.extend(payload["symbols_in_shard"])
        errors.update(payload["errors"])
        for inst,rows in payload["candle_rows"].items():
            if inst in allrows:raise RuntimeError("Duplicate coin across shards")
            allrows[inst]={int(ts):tuple(val) for ts,val in rows.items()}
    print(f"SHARD_SUMMARY={meta} COINS_GOOD={len(allrows)} COINS_MISSING={len(errors)} TOTAL_ELIGIBLE={len(set(allsymbols))}",flush=True)
    if len(allrows)<200:
        raise RuntimeError("Still less than 200 instruments with 5m candles, cannot correctly rank TOP200")

    def fake_list():
        return sorted(allrows.keys())

    def fake_candles(inst,end,oldest):
        return allrows[inst],0

    real_datetime=datetime
    class FrozenClock(real_datetime):
        @classmethod
        def now(cls,tz=None):
            stamp=anchor()
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)

    original.list_swaps=fake_list
    original.candles=fake_candles
    original.datetime=FrozenClock
    original.main()
    path=Path("blofin_top200_repeat_setup_report.json")
    report=json.loads(path.read_text(encoding="utf-8"))
    report["shard_fetch"]={
        "total_live_current_crypto_usdt_instruments":len(set(allsymbols)),
        "successful_instruments":len(allrows),
        "failed_instruments":errors,
        "shard_summary":meta,
        "asof_uk":ASOF}
    # Fix summary: point-in-time rankings within available fetched universe only.
    report["historical_ranking_limitations"]="Daily 24h ranking calculated among successfully fetched current symbols. Excluded unavailable/de-listed assets may change true ranking. This test uses all available successful instruments, not trading histories."
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("FINAL_VERDICT="+json.dumps({
        "fetched":len(allrows),"eligible":len(set(allsymbols)),
        "scanned":report.get("scenarios_scanned"),
        "train_hits":report.get("training_meets_three_trades_daily_and_3pct_mean"),
        "train_validation_hits":report.get("passes_training_and_validation_3pct_mean"),
        "top":report.get("selected_before_holdout",[])[:2],
        "train_only":report.get("train_only_diagnostics_if_no_validated",[])[:2]
    },ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    if len(sys.argv)<2:raise SystemExit("Usage: fetch 0..3 | analyze")
    if sys.argv[1]=="fetch":
        fetch_shard(int(sys.argv[2]))
    elif sys.argv[1]=="analyze":
        analyze()
    else:raise SystemExit("Unknown stage")
