#!/usr/bin/env python3
"""Free, public BloFin USDT futures 5m OHLCV archive; NO private API / trades."""
import argparse, gzip, json, math, random, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

BASE="https://openapi.blofin.com"
CANDLE_MS=300000
DAY_MS=86400000

def utc(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()

def get(path,params=None):
    url=BASE+path+("?"+urllib.parse.urlencode(params) if params else "")
    failure=None
    for attempt in range(6):
        if attempt:
            time.sleep(min(75,4*(2**(attempt-1)))+random.uniform(0,1))
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"BloFinPublicHistoricalStudy/1.0","Accept":"application/json"})
            with urllib.request.urlopen(req,timeout=30) as res:
                obj=json.loads(res.read())
            if str(obj.get("code"))!="0":
                raise ValueError("BloFin code "+str(obj.get("code"))+" "+str(obj.get("msg",""))[:140])
            data=obj.get("data")
            if not isinstance(data,list):
                raise ValueError("Expected list data")
            time.sleep(.32)
            return data
        except (OSError,ValueError,urllib.error.HTTPError) as e:
            failure=e
            print("REQUEST_ERROR",type(e).__name__,str(e)[:160],"attempt",attempt+1,flush=True)
    raise RuntimeError("Public BloFin API unavailable: "+str(failure)[:240])

def select_universe(out,days,limit):
    all_inst=get("/api/v1/market/instruments")
    tickers=get("/api/v1/market/tickers")
    eligible={x.get("instId") for x in all_inst
         if str(x.get("instId","")).endswith("-USDT")
         and str(x.get("assetClass","Crypto")).lower() in ("crypto","")
         and str(x.get("state","live")).lower() not in ("delisted","suspend","offline")}
    ranked=[]
    for t in tickers:
        inst=t.get("instId")
        if inst not in eligible: continue
        try:
            last=float(t.get("last")); op=float(t.get("open24h"))
            if not math.isfinite(last) or not math.isfinite(op) or op<=0: continue
            change=100*(last/op-1)
            ranked.append((change,inst))
        except (TypeError,ValueError,ZeroDivisionError): pass
    ranked.sort(key=lambda x:(-x[0],x[1]))
    selected=[{"rank_at_download":i+1,"inst":inst,"change_24h_pct":round(change,7)}
             for i,(change,inst) in enumerate(ranked[:limit])]
    if not selected: raise RuntimeError("No active ranked USDT instruments from public API")
    cutoff=(int(time.time()*1000)//CANDLE_MS)*CANDLE_MS
    obj={"source":"BloFin public /api/v1/market/instruments and /tickers",
         "data_api":"/api/v1/market/candles","bar":"5m",
         "rank_warning":"TOP200 snapshot NOW, not historical TOP200 at each candle (survivorship bias).",
         "universe_time_utc":utc(cutoff),"cutoff_ms":cutoff,
         "start_ms":cutoff-days*DAY_MS,
         "days_requested":days,"requested_symbols":limit,
         "symbols":selected,"coins_selected":len(selected)}
    path=Path(out);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,indent=2)+"\n",encoding="utf-8")
    print("UNIVERSE",len(selected),"from",len(eligible),"start",utc(obj["start_ms"]),"end",utc(cutoff),flush=True)

def validate_row(row):
    if not isinstance(row,list) or len(row)<9: return None
    try:
        ts=int(row[0]); nums=[float(v) for v in row[1:8]]
        if ts%CANDLE_MS!=0 or any(not math.isfinite(v) for v in nums): return None
        o,h,l,c,vol=nums[:5]
        if min(o,h,l,c)<=0 or vol<0 or h<max(o,c) or l>min(o,c): return None
        if str(row[8])!="1": return None
        return ts
    except (ValueError,TypeError,OverflowError): return None

def download_symbol(inst,start_ms,cutoff_ms,folder,max_pages=30):
    existing={}
    cursor=None;pages=0;total_raw=0;error=None;last_oldest=cutoff_ms
    # Limit 1440 = 5 days of 5m bars per call, ~19 pages / 90 days.
    for _ in range(max_pages):
        try:
            params={"instId":inst,"bar":"5m","limit":"1440"}
            if cursor is not None: params["after"]=str(cursor)
            raw=get("/api/v1/market/candles",params)
            pages+=1;total_raw+=len(raw)
            if not raw: break
            ts_list=[]
            for row in raw:
                if not isinstance(row,list) or not row: continue
                try: ts=int(row[0])
                except (ValueError,TypeError): continue
                ts_list.append(ts)
                if start_ms<=ts<cutoff_ms and validate_row(row) is not None:
                    existing[ts]=row[:9]
            if not ts_list: break
            oldest=min(ts_list)
            if oldest>=last_oldest:
                error="pagination_not_advancing";break
            last_oldest=oldest
            if oldest<=start_ms or len(raw)<2: break
            # BloFin after=ts returns candles strictly older than ts
            cursor=oldest
        except Exception as e:
            error=type(e).__name__+": "+str(e)[:220];break
    rows=[existing[t] for t in sorted(existing)]
    folder.mkdir(parents=True,exist_ok=True)
    dest=folder/(inst+".json.gz")
    with gzip.open(dest,"wt",encoding="utf-8",compresslevel=6) as f:
        json.dump({"instId":inst,"bar":"5m","columns":["ts","o","h","l","c","vol","volCurrency","volCurrencyQuote","confirm"],
            "rows":rows},f,separators=(",",":"))
    expected=(cutoff_ms-start_ms)//CANDLE_MS
    first=int(rows[0][0]) if rows else None
    last=int(rows[-1][0]) if rows else None
    # Full period coverage separately from completeness (exchange gaps possible).
    return {"inst":inst,"rows":len(rows),"pages":pages,"raw_rows":total_raw,
       "first_ms":first,"last_ms":last,
       "first_utc":utc(first) if first else None,
       "last_utc":utc(last) if last else None,
       "age_days":round((cutoff_ms-first)/DAY_MS,3) if first else 0,
       "coverage_pct":round(100*len(rows)/expected,3) if expected else 0,
       "full_window":bool(first and first<=start_ms+CANDLE_MS and last>=cutoff_ms-2*CANDLE_MS),
       "error":error,"artifact_file":dest.name}

def shard(universe,idx,shards,out):
    obj=json.loads(Path(universe).read_text())
    folder=Path(out);(folder/"candles").mkdir(parents=True,exist_ok=True)
    selected=[x for i,x in enumerate(obj["symbols"]) if i%shards==idx]
    statuses=[]
    for count,item in enumerate(selected,1):
        inst=item["inst"]
        print("DOWNLOAD",idx,inst,count,"/",len(selected),flush=True)
        statuses.append(download_symbol(inst,obj["start_ms"],obj["cutoff_ms"],folder/"candles"))
    meta={"shard":idx,"shards":shards,"universe_time_utc":obj["universe_time_utc"],
          "requested_days":obj["days_requested"],"start_ms":obj["start_ms"],
          "cutoff_ms":obj["cutoff_ms"],"coins_requested_in_shard":len(selected),
          "download":statuses}
    (folder/"meta").mkdir(parents=True,exist_ok=True)
    (folder/"meta"/("shard-"+str(idx)+".json")).write_text(json.dumps(meta,indent=2)+"\n")
    print("SHARD_FINISHED",idx,"candles",sum(x["rows"] for x in statuses),
          "full_window_coins",sum(x["full_window"] for x in statuses),flush=True)

def summary(universe,meta_dir,out):
    u=json.loads(Path(universe).read_text())
    metas=[json.loads(f.read_text()) for f in sorted(Path(meta_dir).glob("shard-*.json"))]
    results=[x for s in metas for x in s["download"]]
    seen=set();dupes=[]
    for x in results:
        if x["inst"] in seen: dupes.append(x["inst"])
        seen.add(x["inst"])
    missing=sorted({x["inst"] for x in u["symbols"]}-seen)
    outobj={"status":"COMPLETE" if not missing and not dupes and all(x["error"] is None for x in results) else "INCOMPLETE",
       "source":"Free public BloFin API, no authentication, no trading keys",
       "period_start_utc":utc(u["start_ms"]),"period_end_utc":utc(u["cutoff_ms"]),
       "days_requested":u["days_requested"],"interval":"5m",
       "requested_coins":u["requested_symbols"],"selected_coins":len(u["symbols"]),
       "reported_coins":len(results),"total_candles_downloaded":sum(x["rows"] for x in results),
       "coins_with_90d_window":sum(x["full_window"] for x in results),
       "coins_coverage_at_least_95pct":sum(x["coverage_pct"]>=95 for x in results),
       "missing_coins":missing,"duplicate_coins":dupes,
       "issues":[{"inst":x["inst"],"error":x["error"] or ("short_history" if not x["full_window"] else "data_gaps"),
                  "rows":x["rows"],"coverage_pct":x["coverage_pct"]}
                 for x in results if x["error"] or not x["full_window"] or x["coverage_pct"]<95],
       "limitations":["Current top gainers are NOT historical TOP200 composition.",
          "5m candle OHLCV cannot reconstruct intra-candle fill order or historic order book.",
          "Data compressed in GitHub Actions artifacts, not Git history; artifact retention applies.",
          "No backtest or profitable-filter claim from download alone."],
       "universe":u["symbols"],"per_symbol":results}
    path=Path(out);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(outobj,indent=2)+"\n")
    print("SUMMARY",outobj["status"],"coins",len(results),
       "candles",outobj["total_candles_downloaded"],"full90d",outobj["coins_with_90d_window"],
       "missing",len(missing),flush=True)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("action",choices=["universe","shard","summary"])
    ap.add_argument("--out",required=True)
    ap.add_argument("--universe",default="universe.json")
    ap.add_argument("--meta",default="data/meta")
    ap.add_argument("--days",type=int,default=90)
    ap.add_argument("--limit",type=int,default=200)
    ap.add_argument("--idx",type=int,default=0)
    ap.add_argument("--shards",type=int,default=10)
    args=ap.parse_args()
    if args.action=="universe": select_universe(args.out,args.days,args.limit)
    elif args.action=="shard": shard(args.universe,args.idx,args.shards,args.out)
    else: summary(args.universe,args.meta,args.out)

if __name__=="__main__":
    main()
