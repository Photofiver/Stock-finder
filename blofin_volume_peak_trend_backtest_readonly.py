"""READ ONLY: test user's strongest-volume-bar trend rule on BloFin closed 5m OHLCV.

Dominant volume anchor resets on reversal. GREEN if close>open, RED if
close<open; doji ignored. On green trend, only RED volume STRICTLY GREATER
than peak green volume flips to SHORT (and reverse). A same-trend stronger
bar raises the anchor. Strictly uses bars already closed at time of signal.

No private endpoints, secrets, trade calls or writes to LIVE bot files.
"""
import collections
import concurrent.futures
import datetime as dt
import json
import statistics
import time
from pathlib import Path
import requests

import blofin_trendline_backtest_readonly as base

FIVE=base.FIVE
WINDOW_HOURS=72
WARMUP_BARS=120
NOTIONAL=20.0
FEE_ROUNDTRIP_PCT=0.12
CLIENT=requests.Session()


def get_page(inst, after):
    for attempt in range(5):
        try:
            resp=CLIENT.get("https://openapi.blofin.com/api/v1/market/candles",
                            params={"instId":inst,"bar":"5m","after":str(after),"limit":"240"},
                            timeout=25)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(attempt+1)
                continue
            resp.raise_for_status()
            payload=resp.json()
            if str(payload.get("code"))!="0":
                raise ValueError("BloFin nonzero code")
            rows=[]
            for raw in payload.get("data",[]):
                try:
                    if len(raw)<9 or str(raw[8])!="1":
                        continue
                    rows.append({"ts":int(raw[0]),"o":float(raw[1]),
                                 "h":float(raw[2]),"l":float(raw[3]),
                                 "c":float(raw[4]),"v":float(raw[5])})
                except (IndexError,TypeError,ValueError):
                    continue
            return rows
        except (ValueError,requests.RequestException) as exc:
            if attempt == 4:
                raise RuntimeError(type(exc).__name__)
            time.sleep(attempt+1)
    raise RuntimeError("No candle data")


def fetch_candles(inst, end, earliest):
    retrieved={}
    after=end
    pages=0
    for _ in range(8):
        rows=get_page(inst,after)
        pages+=1
        if not rows:
            break
        for row in rows:
            if row["ts"]+FIVE <= end:
                retrieved[row["ts"]]=row
        oldest=min(x["ts"] for x in rows)
        if oldest<earliest or oldest>=after:
            break
        after=oldest
    sorted_bars=sorted(retrieved.values(),key=lambda b:b["ts"])
    chunks=[]
    chunk=[]
    for b in sorted_bars:
        if chunk and b["ts"]-chunk[-1]["ts"]!=FIVE:
            if len(chunk)>=45:
                chunks.append(chunk)
            chunk=[]
        chunk.append(b)
    if len(chunk)>=45:
        chunks.append(chunk)
    return chunks,pages


def classify_candle(bar):
    if bar["c"]>bar["o"]: return "LONG"
    if bar["c"]<bar["o"]: return "SHORT"
    return None


def regime_series(bars):
    """Output (regime, flip) for each CLOSED candle; returns NO flip for seed."""
    direction=None
    peak=0.0
    result=[]
    for b in bars:
        color=classify_candle(b)
        vol=b["v"]
        flip=False
        if color and vol>0:
            if direction is None:
                direction=color
                peak=vol
            elif color==direction:
                if vol>peak:
                    peak=vol
            elif vol>peak:
                direction=color
                peak=vol
                flip=True
        result.append((direction,flip,peak))
    return result


def ma_cross(bars,i):
    if i<20:
        return None
    closes=[b["c"] for b in bars[i-20:i+1]]
    former10=sum(closes[-11:-1])/10
    former20=sum(closes[-21:-1])/20
    latest10=sum(closes[-10:])/10
    latest20=sum(closes[-20:])/20
    if former10<=former20 and latest10>latest20:
        return "LONG"
    if former10>=former20 and latest10<latest20:
        return "SHORT"
    return None


def stats(events):
    n=len(events)
    gross=sum(e["pct"]*NOTIONAL/100 for e in events)
    fees=n*NOTIONAL*FEE_ROUNDTRIP_PCT/100
    gross_winners=sum(e["pct"]>0 for e in events)
    after_fee_winners=sum(e["pct"]>FEE_ROUNDTRIP_PCT for e in events)
    return {"count":n,"wins_gross":gross_winners,
            "win_rate_gross_pct":round(100*gross_winners/n,2) if n else None,
            "wins_after_estimated_fees":after_fee_winners,
            "win_rate_after_fees_pct":round(100*after_fee_winners/n,2) if n else None,
            "gross_pnl_usdt":round(gross,4),
            "estimated_fees_usdt":round(fees,4),
            "net_after_estimated_fees_usdt":round(gross-fees,4),
            "tp":sum(e["reason"]=="TP" for e in events),
            "sl":sum(e["reason"] in ("SL","AMBIGUOUS") for e in events),
            "time_exit":sum(e["reason"]=="TIME" for e in events)}


def one_at_a_time(events):
    selected=[]
    until=0
    for e in sorted(events,key=lambda e:(e["at_ms"],e["inst"])):
        if e["at_ms"]<until: continue
        selected.append(e)
        until=e["end_ms"]
    return selected


def actual_stats(trades):
    withgross=[t for t in trades if t.get("gross_pnl_usdt") is not None]
    withnet=[t for t in withgross if t.get("net_pnl_usdt") is not None]
    gross=sum(float(t["gross_pnl_usdt"]) for t in withgross)
    net=sum(float(t["net_pnl_usdt"]) for t in withnet)
    return {"count":len(trades),
            "gross_scored":len(withgross),
            "gross_wins":sum(float(t["gross_pnl_usdt"])>0 for t in withgross),
            "gross_win_rate_pct":round(100*sum(float(t["gross_pnl_usdt"])>0 for t in withgross)/len(withgross),2) if withgross else None,
            "gross_pnl_usdt":round(gross,5),
            "net_scored":len(withnet),
            "net_pnl_usdt":round(net,5) if len(withnet)==len(trades) else None}


def main():
    state=json.loads(Path("blofin_live_state.json").read_text(encoding="utf-8"))
    end=(int(time.time()*1000)//FIVE)*FIVE
    start=end-WINDOW_HOURS*60*60_000
    start_symbols=[]
    actual=[]
    for t in state.get("trade_history",[]):
        if not isinstance(t,dict):
            continue
        try:
            opened=int(t.get("opened_ms") or 0)
            rank=int(t.get("signal_rank") or 0)
        except (TypeError,ValueError):
            continue
        if not(start<=opened<end and 51<=rank<=100 and t.get("inst")):
            continue
        if t["inst"] not in start_symbols:
            start_symbols.append(t["inst"])
        actual.append(t)
    if not actual:
        raise RuntimeError("No actual rank51-100 trades in 72h")
    if len(start_symbols)>100:
        raise RuntimeError("Too many symbols")
    first=start-WARMUP_BARS*FIVE
    candles={}
    errors={}
    times=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures={pool.submit(fetch_candles,inst,end,first):inst for inst in start_symbols}
        for future in concurrent.futures.as_completed(futures):
            inst=futures[future]
            try:
                chunks,_=future.result()
                if chunks:
                    candles[inst]=chunks
                else:
                    errors[inst]="No contiguous history"
            except Exception as exc:
                errors[inst]=str(exc)
    print(f"FETCHED_COINS={len(candles)} FAILED={len(errors)} SECONDS={time.monotonic()-times:.1f}",flush=True)
    if len(candles)<3:
        raise RuntimeError("Insufficient history")

    lookup={}
    variants=collections.defaultdict(list)
    switches=collections.Counter()
    for inst,chunks in candles.items():
        bytime={}
        for bars in chunks:
            states=regime_series(bars)
            for i,b in enumerate(bars):
                close_ms=b["ts"]+FIVE
                direction,flip,peak=states[i]
                if start-FIVE<=close_ms<=end:
                    bytime[close_ms]=direction
                if i<25 or not(start<=close_ms<end):
                    continue
                side=ma_cross(bars,i)
                # All rules are known at candle close. Simulated entry at NEXT bar open.
                candidates=[]
                if side:
                    candidates.append(("SMA_ONLY",side))
                    if direction==side:
                        candidates.append(("SMA_WITH_VOLUME",side))
                if flip and direction:
                    candidates.append(("VOLUME_FLIPS",direction))
                    switches[direction]+=1
                for variant,signal_side in candidates:
                    outcome=base.outcome(bars,i,signal_side)
                    if not outcome:
                        continue
                    variants[variant].append({"at_ms":close_ms,"inst":inst,"side":signal_side,**outcome})
        lookup[inst]=bytime

    actual_matches=[]
    for trade in actual:
        inst=trade["inst"]
        last_close=int(trade["opened_ms"])//FIVE*FIVE
        predicted=lookup.get(inst,{}).get(last_close)
        if not predicted:
            continue
        if trade.get("gross_pnl_usdt") is None:
            continue
        actual_matches.append({**trade,"regime":predicted,
                               "aligned":trade.get("side")==predicted})

    summary={}
    for variant in ("VOLUME_FLIPS","SMA_ONLY","SMA_WITH_VOLUME"):
        all_entries=sorted(variants[variant],key=lambda x:(x["at_ms"],x["inst"]))
        chosen=one_at_a_time(all_entries)
        summary[variant]={"all_signals":stats(all_entries),
                          "one_position_at_a_time":stats(chosen),
                          "first_entries":all_entries[:5]}

    report={
        "generated_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
        "window_utc":{"start":dt.datetime.fromtimestamp(start/1000,dt.timezone.utc).isoformat(),
                      "end":dt.datetime.fromtimestamp(end/1000,dt.timezone.utc).isoformat()},
        "coins":len(candles),"coins_requested":len(start_symbols),
        "missing_coins":errors,
        "historical_trades_rank_51_to_100":len(actual),
        "actual_trades_with_known_volume_regime":len(actual_matches),
        "actual_trade_filter":{
            "all_evaluable":actual_stats(actual_matches),
            "aligned_only":actual_stats([t for t in actual_matches if t["aligned"]]),
            "blocked_opposite_direction":actual_stats([t for t in actual_matches if not t["aligned"]]),
            "not_evaluable_count":len(actual)-len(actual_matches)},
        "switches_per_direction":dict(switches),
        "strategies":summary,
        "rule":"Opposite-color closed 5m candle volume must be STRICTLY greater than the maximum same-color volume reached since beginning of current regime. Max updated only with same-color bars, reset on switch. Doji ignored.",
        "backtest":"Public BloFin 5m OHLCV with historical coin set selected from actual bot trades, not historical TOP ranks. Next 5m open, first TP1% or SL0.25% or 10min. SL-first if intra-bar both hit. One global position for concurrency comparison; 20 USDT flat simulated notional.",
        "limits":"SMA_ONLY is raw SMA10/20 cross and NOT full LIVE filters or actual LIVE trade simulator. No spread/slippage/orderbook/variable MA10 stops. Fee 0.12% round trip estimated. Historical backtest does not prove future performance; multiple simultaneous entries resolved alphabetically."
    }
    Path("volume_peak_trend_backtest_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("VOLUME_PEAK_RESULT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    main()
