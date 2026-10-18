"""READ ONLY last-hour TOP10 public BloFin 5m replay of frozen selected SHORT rule.
No private keys, no account info, no trading endpoints or LIVE edits.

TOP10 at run time according to BloFin public 24h gain tickers, not reconstructed
point-in-time historical TOP10. Closed 5m bars only.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import json,time,statistics
import blofin_top200_repeat_setup_research as api

F=300000
UK=ZoneInfo("Europe/London")
FEE=0.12
def fmt(ms):return datetime.fromtimestamp(ms/1000,UK).strftime("%Y-%m-%d %H:%M")
def pchange(a,b):return 100*(a/b-1) if b and b>0 else None
def color(b):return "RED" if b[3]<b[0] else "GREEN" if b[3]>b[0] else "DOJI"
def avg10(rows,ts):
    bs=[rows.get(ts-j*F) for j in range(1,11)]
    return statistics.fmean(b[4] for b in bs) if all(x is not None for x in bs) else None
def completed15(rows,ts):
    block=3*F
    last=((ts+F)//block-1)*block
    end=rows.get(last+2*F)
    prev=rows.get(last-F)
    return pchange(end[3],prev[3]) if end and prev else None
def run():
    started=int(time.time()*1000)
    # Floor to last 5m boundary - candles before it are confirmed.
    anchor=started//F*F
    start=anchor-12*F
    instruments=set(api.list_swaps())
    tickers=api.get_data("/api/v1/market/tickers")
    ranking=[]
    for row in tickers:
        inst=str(row.get("instId") or "")
        if inst not in instruments:continue
        try:
            a=float(row["last"]);b=float(row["open24h"])
            if a>0 and b>0:ranking.append((pchange(a,b),inst))
        except (TypeError,ValueError,KeyError):pass
    ranking.sort(key=lambda x:(-x[0],x[1]))
    if len(ranking)<200:raise RuntimeError(f"Only {len(ranking)} tickers available")
    chosen=ranking[:10]
    print("TOP10_SNAPSHOT="+json.dumps({"asof_uk":fmt(started),"last_hour_uk":[fmt(start),fmt(anchor)],
            "eligible_markets":len(instruments),"ranked_markets":len(ranking),
            "top10":[{"rank":i,"coin":inst,"gain24_pct":round(gain,4)}
                 for i,(gain,inst) in enumerate(chosen,1)]},separators=(",",":")),flush=True)
    ids=[x for _,x in chosen]
    if "BTC-USDT" not in ids:ids.append("BTC-USDT")
    api.MIN_CALL_GAP=.13
    oldest=start-200*F
    def fetch_coin(inst):
        series,pages=api.candles(inst,anchor,oldest)
        if series.get(start-30*F) is None or series.get(anchor-F) is None:
            raise RuntimeError(f"Missing required candles for {inst}, rows={len(series)}")
        return inst,series,pages
    sets={};err={};pages=0
    with ThreadPoolExecutor(max_workers=5) as exe:
        fs={exe.submit(fetch_coin,inst):inst for inst in ids}
        for fut in as_completed(fs):
            inst=fs[fut]
            try:
                sym,s,count=fut.result()
                sets[sym]=s;pages+=count
            except Exception as exc:
                err[inst]=f"{type(exc).__name__}: {str(exc)[:180]}"
    if "BTC-USDT" not in sets:raise RuntimeError("Missing BTC confirmation candles "+str(err))
    print("OHLCV_COVERAGE="+json.dumps({"downloaded":len(sets),"request_pages":pages,
            "errors":err},separators=(",",":")),flush=True)
    trades=[];open_positions=[];all_coin=[]
    for rank,(gain,inst) in enumerate(chosen,1):
        if inst not in sets:
            all_coin.append({"coin":inst,"rank":rank,"error":err.get(inst,"missing OHLCV")})
            continue
        rows=sets[inst];btc=sets["BTC-USDT"]
        counters=Counter()
        pos=None
        opened=[]
        for ts in range(start-F,anchor,F):
            cur=rows.get(ts);prior=rows.get(ts-F)
            if cur is None or prior is None:
                counters["missing_bar"]+=1;continue
            mean=avg10(rows,ts)
            if mean is None or mean<=0:
                counters["missing_mean10"]+=1;continue
            qualified=cur[4]<=mean
            if pos and ts>=pos["entry_ms"] and qualified:
                if color(cur)=="RED":
                    pos["last_red_vol"]=cur[4]
                elif color(cur)=="GREEN" and cur[4]>pos["last_red_vol"]:
                    nextbar=rows.get(ts+F)
                    if nextbar is not None and ts+F<=anchor:
                        gross=100*(1-nextbar[0]/pos["entry_price"])
                        outcome="WIN" if gross>0 else "LOSS" if gross<0 else "FLAT"
                        trades.append({"coin":inst,"rank":rank,"entry_uk":fmt(pos["entry_ms"]),
                            "exit_uk":fmt(ts+F),"gross_pct":round(gross,6),
                            "net_after_fee_pct":round(gross-FEE,6),"outcome":outcome})
                        counters["closed"]+=1
                        pos=None
            if pos is not None:continue
            if not (start<=ts+F<anchor):continue
            if color(prior)!="GREEN" or color(cur)!="RED":continue
            counters["red_after_green"]+=1
            if prior[4]<=0 or not 3<=cur[4]/prior[4]<4:continue
            counters["vol_3to4"]+=1
            if not qualified:continue
            counters["vol_under_mean10"]+=1
            past=rows.get(ts-30*F)
            if past is None:continue
            if pchange(cur[3],past[3])<0:continue
            counters["prior150_up"]+=1
            coin15=completed15(rows,ts)
            if coin15 is None or coin15<-.10322961:continue
            counters["coin15_ok"]+=1
            if cur[1]<=prior[1] and cur[2]>=prior[2]:continue
            counters["not_inside"]+=1
            bt0=btc.get(ts);bt3=btc.get(ts-3*F)
            if bt0 is None or bt3 is None:continue
            if pchange(bt0[3],bt3[3])>.062055813:continue
            counters["btc15_ok"]+=1
            nextbar=rows.get(ts+F)
            if nextbar is None:
                counters["missing_next_open"]+=1;continue
            pos={"entry_ms":ts+F,"entry_price":nextbar[0],"last_red_vol":cur[4]}
            opened.append({"coin":inst,"rank":rank,"signal_uk":fmt(ts+F),
                "btc_last15_pct":round(pchange(bt0[3],bt3[3]),4),
                "coin_completed15_pct":round(coin15,4)})
            counters["entered"]+=1
        if pos:
            open_positions.append({"coin":inst,"rank":rank,"entry_uk":fmt(pos["entry_ms"]),
                                   "entry_price":pos["entry_price"],"status":"OPEN_OR_UNRESOLVED"})
        all_coin.append({"coin":inst,"rank":rank,"gain24_pct":round(gain,4),
                         "saved_candle_count":len(rows),"checks":dict(counters),"entries":opened})
    counts=Counter(x["outcome"] for x in trades)
    gross=sum(t["gross_pct"] for t in trades)
    result={"time_checked_uk":fmt(started),"window_uk":[fmt(start),fmt(anchor)],
       "rank_method":"Snapshot NOW of top 10 public BloFin 24h percentage gainers; historical hourly ranks not archived per minute.",
       "top10":[{"coin":sym,"rank":i,"gain24_pct":round(ret,4)} for i,(ret,sym) in enumerate(chosen,1)],
       "market_count":len(instruments),"complete_candles_loaded":len(sets),"errors":err,
       "rule":"SHORT 5m RED after GREEN; volume in [3,4)x prior GREEN and <=prev10 average; 150m pretrend >=0; completed15m >= -0.10322961%; candle not inside previous; BTC 15m <= +0.062055813%; entry next 5m open; exit next open after stronger qualifying GREEN > last qualifying RED volume; commissions .12% per round trip.",
       "all_coin_checks":all_coin,
       "complete_trades":trades,"still_open_or_unresolved":open_positions,
       "closed_wins":counts["WIN"],"closed_losses":counts["LOSS"],"flats":counts["FLAT"],
       "open_count":len(open_positions),"closed_count":len(trades),
       "success_rate_pct":round(100*counts["WIN"]/(counts["WIN"]+counts["LOSS"]),2) if counts["WIN"]+counts["LOSS"] else None,
       "gross_sum_pct_points":round(gross,6),
       "net_sum_pct_points":round(gross-.12*len(trades),6),
       "estimated_net_usdt_per20_notional":round(20*(gross-.12*len(trades))/100,6),
       "disclaimer":"No forced close at end of hour; any open positions are NOT wins/losses. Trading fees assumed, no slippage/spread/funding; sums of percentages not account ROI. Current TOP10 snapshot can differ from historical TOP10 within window, and 100% earlier rule in-sample cannot establish accuracy."
       }
    Path("short_top10_last_hour_readonly_report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print("LAST_HOUR_TOP10_RESULT="+json.dumps({
        k:v for k,v in result.items() if k not in ("all_coin_checks","complete_trades","still_open_or_unresolved")
    },ensure_ascii=False,separators=(",",":")),flush=True)
    print("LAST_HOUR_DETAILS="+json.dumps({"coins":all_coin,"trades":trades,
        "open":open_positions},ensure_ascii=False,separators=(",",":")),flush=True)
if __name__=="__main__":run()
