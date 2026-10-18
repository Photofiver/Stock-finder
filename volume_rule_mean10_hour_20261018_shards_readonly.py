"""Read-only four-shard collection for frozen October 18 volume/mean10 comparison."""
import concurrent.futures
import gzip
import json
import sys
import time
from pathlib import Path

import blofin_top200_repeat_setup_research as source
import volume_rule_mean10_hour_20261018_readonly as test

SHARDS=4
source.MIN_CALL_GAP=0.62

def acquire(shard):
    allsyms=source.list_swaps()
    these=allsyms[shard::SHARDS]
    rows={}
    bad={}
    def fetch(inst):
        data,pages=source.candles(inst,test.CURSOR,test.EARLIEST)
        return data
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(fetch,s):s for s in these}
        for n,f in enumerate(concurrent.futures.as_completed(futures),1):
            inst=futures[f]
            try:
                result=f.result()
                if len(result)>=290:
                    rows[inst]=result
                else:
                    bad[inst]="short "+str(len(result))
            except Exception as exc:
                bad[inst]=str(exc)[:150]
            if n%25==0:
                print("SHARD_PROGRESS="+json.dumps({"shard":shard,"done":n,"good":len(rows),"bad":len(bad)}),flush=True)
    # Retry once more at a lower rate for the missing instruments.
    source.MIN_CALL_GAP=0.9
    for inst in list(bad):
        try:
            result=fetch(inst)
            if len(result)>=290:
                rows[inst]=result
                del bad[inst]
        except Exception as e:
            bad[inst]=str(e)[:150]
    file=Path("mean10_shard_"+str(shard)+".json.gz")
    with gzip.open(file,"wt",encoding="utf-8") as f:
        json.dump({"shard":shard,"total":len(allsyms),
                   "symbols":these,"data":rows,"failed":bad},f,separators=(",",":"))
    print("SHARD_FINAL="+json.dumps({"shard":shard,"symbols":len(these),
      "good":len(rows),"failed":len(bad),"errors":bad}),flush=True)

def analyze():
    combined={}
    allfails={}
    total=0
    for shard in range(SHARDS):
        file=Path("shards")/("mean10_shard_"+str(shard)+".json.gz")
        with gzip.open(file,"rt",encoding="utf-8") as f:
            dataset=json.load(f)
        if dataset["shard"]!=shard:raise RuntimeError("Mismatch shard")
        total=dataset["total"]
        allfails.update(dataset["failed"])
        for inst,rows in dataset["data"].items():
            if inst in combined:raise RuntimeError("Duplicate "+inst)
            combined[inst]={int(key):tuple(value) for key,value in rows.items()}
    print("COMPLETE_DATA="+json.dumps({"total":total,"good":len(combined),
      "missing":len(allfails)}),flush=True)
    if len(combined)<300:
        raise RuntimeError("Below 300 symbols, ranking would be unreliable")
    test.analyse(combined,allfails,total)

if __name__=="__main__":
    if len(sys.argv)>1 and sys.argv[1]=="fetch":
        acquire(int(sys.argv[2]))
    elif len(sys.argv)>1 and sys.argv[1]=="analyse":
        analyze()
    else:
        raise RuntimeError("fetch N | analyse")
