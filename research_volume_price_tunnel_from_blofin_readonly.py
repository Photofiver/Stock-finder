"""READ-ONLY replay of adaptive LIVE volume price tunnel on real old JSON trades.

Original trade RESULTS and fills come only from blofin_live_state.json.
Historical confirmed 5m OHLCV is read from PUBLIC BloFin candle API only,
at the ORIGINAL saved signal time; never sends an order, modifies LIVE bot
or requests an authenticated endpoint. No fabricated candles or outcomes.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, Counter
from pathlib import Path
import json
import math
import time
import requests

import blofin_volume_flip_both_live as live

FIVE=300_000
BASE="https://openapi.blofin.com/api/v1/market/candles"
STRATEGY=live.STRATEGY


def candles_for_coin(coin, latest, earliest):
    """Page backwards until the entire pre-signal window is covered."""
    result = {}
    cursor = latest + FIVE
    needed = earliest - 26*FIVE
    seen_pages = 0
    with requests.Session() as client:
        for page in range(6):
            data = None
            for attempt in range(5):
                try:
                    r = client.get(BASE, params={
                        "instId":coin,"bar":"5m","after":str(cursor),"limit":"240"
                    },timeout=22)
                    if r.status_code == 429 or r.status_code >= 500:
                        time.sleep(1.2*(attempt+1))
                        continue
                    r.raise_for_status()
                    body=r.json()
                    if str(body.get("code"))!="0":
                        raise RuntimeError("BloFin API nonzero response")
                    data=body.get("data") or []
                    break
                except (ValueError, RuntimeError, requests.RequestException):
                    if attempt==4:
                        raise
                    time.sleep(.8*(attempt+1))
            if not data:break
            seen_pages+=1
            oldest=None
            for b in data:
                try:
                    if not isinstance(b,list) or len(b)<9 or str(b[8])!="1":
                        continue
                    ts=int(b[0])
                    if ts>=cursor:
                        continue
                    row={"ts":ts,"o":float(b[1]),"h":float(b[2]),
                         "l":float(b[3]),"c":float(b[4]),"v":float(b[5])}
                    if ts+FIVE<=latest and all(math.isfinite(row[z])
                            for z in ("o","h","l","c","v")):
                        result[ts]=row
                    oldest=ts if oldest is None else min(oldest,ts)
                except (ValueError,TypeError,KeyError):
                    continue
            if oldest is None or oldest>=cursor or oldest<=needed:
                break
            cursor=oldest
    return result,seen_pages


def historical_filters(t):
    s=t.get("signal_snapshot") or {}
    try:
        ma=float(s["signal_mean20_prev"])
        strength=float(s["signal_volume"])/ma
        if not .90<=strength<1.0:
            return False
        if t["side"]=="LONG":
            return (1<=int(s["rank"])<=50 and
                    float(s["previous_mean20_prev"])>ma)
        q=s["pre_order_quote"]
        return (float(q["adverse_gap_pct"])>=.05 and
                float(q["spread_pct"])<=.05)
    except (ValueError,TypeError,KeyError,ZeroDivisionError):
        return False


def tally(a):
    a=list(a)
    return {
        "n":len(a),
        "win":sum(x["result"]=="WIN" for x in a),
        "loss":sum(x["result"]=="LOSS" for x in a),
        "sum_net_usdt":round(sum(float(x["net_pnl_usdt"]) for x in a),6)
    }


def main():
    state=json.loads(Path("blofin_live_state.json").read_text())
    closed=[x for x in state.get("trade_history",[])
            if x.get("result") in ("WIN","LOSS")]
    recent=closed[-60:]
    eligible=[x for x in recent if x.get("strategy")==STRATEGY
              and x.get("side") in ("LONG","SHORT")
              and (x.get("signal_snapshot") or {}).get("signal_close_ms")]
    by_coin=defaultdict(list)
    for x in eligible:
        by_coin[x["inst"]].append(int(x["signal_snapshot"]["signal_close_ms"]))
    market={}
    errors={}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs={
            pool.submit(candles_for_coin,inst,max(stamps),min(stamps)):inst
            for inst,stamps in by_coin.items()
        }
        for future in as_completed(futs):
            inst=futs[future]
            try:
                market[inst],pages=future.result()
                print("FETCH="+json.dumps({
                    "inst":inst,"rows":len(market[inst]),"pages":pages
                }),flush=True)
            except Exception as e:
                errors[inst]=str(type(e).__name__)+": "+str(e)[:100]
                print("FETCH_ERROR="+json.dumps({"inst":inst,"error":errors[inst]}),
                      flush=True)

    assessed=[]
    for t in eligible:
        s=t["signal_snapshot"]
        close=int(s["signal_close_ms"])
        known=list(market.get(t["inst"],{}).values())
        tunnel=live.volume_price_tunnel_from_bars(known,close,t["side"])
        row={
            "coin":t["inst"],"signal_ms":close,"side":t["side"],
            "result":t["result"],"net_pnl_usdt":float(t["net_pnl_usdt"]),
            "old_filters_at_entry":historical_filters(t),
            "tunnel_status":tunnel["status"],
            "tunnel_reason":tunnel.get("reason"),
            "tunnel_pass":tunnel["breakout_confirmed"],
            "upper":tunnel.get("upper_price"),
            "lower":tunnel.get("lower_price"),
            "anchor_upper_volumes":[v["volume"] for v in tunnel.get("upper_anchors",[])],
            "anchor_lower_volumes":[v["volume"] for v in tunnel.get("lower_anchors",[])],
        }
        assessed.append(row)

    valid=[x for x in assessed if x["tunnel_status"]=="VALID"]
    passes=[x for x in valid if x["tunnel_pass"]]
    both=[x for x in passes if x["old_filters_at_entry"]]
    report={
        "source":"Actual JSON completed trade outcomes plus BloFin public confirmed 5m candles, point-in-time tunnel calculation",
        "limits":"Retrospective subset filter only, not a new entry-generation backtest; excludes new signals not previously traded; does not reprice fills, predict profit, or reconstruct historical ranking/orderbook accurately.",
        "json_history_total":len(closed),
        "last60":tally(recent),
        "last60_volume_strategy":tally(eligible),
        "coins_requested":len(by_coin),
        "coins_fetched":len(market),
        "coins_failed":errors,
        "tunnel_verifiable":tally(valid),
        "tunnel_passed":tally(passes),
        "tunnel_plus_recorded_existing_filters":tally(both),
        "tunnel_missing_data":len(assessed)-len(valid),
        "details":assessed,
    }
    print("TUNNEL_PUBLIC_CANDLES_RESULT="+json.dumps(report,ensure_ascii=False),
          flush=True)


if __name__=="__main__":
    main()
