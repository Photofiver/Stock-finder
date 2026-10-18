"""TRUE read-only replay of user's 3-4x SHORT rule from SAVED GitHub JSON scan reviews.

Uses ONLY archived next-candle-review 5m open/high/low/close and historical
volume saved inside JSON. Never substitutes stale 'candle' snapshots (the
archive includes old snapshots mistakenly reused at later scan times).
Cannot use exchange or downloaded OHLCV. Signals use only past data.
Same volume reversal exit, next 5m candle open. All coin positions independent.

Conditions discovered from 37/26 selection: prior150min price>=0;
BTC last15<=+.062055813%; completed coin 15m>=-.10322961%;
5m bar not fully inside previous; red after green, volume 3<=x<4,
volume<=prior10 mean; point-in-time stored rank<=200.
"""
from __future__ import annotations
from collections import Counter,defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import json, math, statistics, gc
F=300_000
UK=ZoneInfo("Europe/London")
ROUNDTRIP_FEE=.12
def uk(ms):return datetime.fromtimestamp(ms/1000,UK).strftime("%Y-%m-%d %H:%M")
def pct(a,b):
    return 100*(a/b-1) if b and b>0 else None
def side(b):
    return "RED" if b[3]<b[0] else "GREEN" if b[3]>b[0] else "DOJI"
def calc15(rows,ts):
    # replicate frozen 15m "completed" function
    units=3;block=F*units
    end=((ts+F)//block-1)*block+(units-1)*F
    x=rows.get(end);y=rows.get(end-F*units)
    if x is None or y is None:return None
    return pct(x[3],y[3])
def valid_candles(rows,ts,prior=31):
    return all(ts-j*F in rows for j in range(prior+1))
def main():
    paths=sorted(Path("blofin_live_scans").glob("live_watch_history_5m*.json"))
    if len(paths)<10:raise RuntimeError("Did not checkout expected saved 5m JSON archives")
    bars=defaultdict(dict)
    ranks=defaultdict(dict)
    file_stats=[]
    conflicts=0
    seen_reviews=0
    valid_reviews=0
    stale=0
    for idx,p in enumerate(paths):
        blob=json.loads(p.read_text(encoding="utf-8"))
        scans=blob.get("scans") or []
        counts=Counter();times=[]
        for s in scans:
            at=int(s.get("signal_close_ms") or 0)
            if not at:continue
            times.append(at)
            names=s.get("instruments") or []
            counts["scan_instruments"]+=len(names)
            for it in names:
                coin=str(it.get("inst") or "").strip()
                if not coin:continue
                row_at=int(it.get("signal_close_ms") or 0)
                if row_at and row_at!=at:stale+=1
                # rank recorded at this scan, but stale entries are discarded
                if row_at==at:
                    rank=it.get("rank")
                    if isinstance(rank,int) and 1<=rank<=200:
                        ranks[at-F][coin]=rank  # scan close time at = bar start + F
                nxt=it.get("next_candle_review") or {}
                if nxt.get("status")!="DONE":continue
                seen_reviews+=1
                nextclose=int(nxt.get("next_candle_close_ms") or 0)
                # review next5m candle must actually follow this scan
                if nextclose!=at+F:counts["wrong_review_timestamp"]+=1;continue
                volume=(nxt.get("indicators_after_5m") or {}).get("volume")
                keys=("open","high","low","close")
                try:
                    o,h,l,c=[float(nxt[z]) for z in keys]
                    v=float(volume)
                except (TypeError,ValueError,KeyError):counts["incomplete_review"]+=1;continue
                if not(o>0 and h>=max(o,c) and l<=min(o,c) and v>=0):
                    counts["nonsensical_review"]+=1;continue
                b=(o,h,l,c,v)
                close_ts=nextclose-F  # start ms of the NEXT bar
                prev=bars[coin].get(close_ts)
                if prev is not None and any(not math.isclose(a,z,rel_tol=1e-9,abs_tol=1e-12) for a,z in zip(prev,b)):
                    conflicts+=1
                    continue
                bars[coin][close_ts]=b
                valid_reviews+=1
                counts["accepted"]+=1
        file_stats.append({"file":p.name,"scans":len(scans),
            "start_uk":uk(min(times)) if times else None,
            "end_uk":uk(max(times)) if times else None,
            "accepted_candles":counts["accepted"],
            "bad_review_time":counts["wrong_review_timestamp"],
            "incomplete_review":counts["incomplete_review"],
            "nonsensical":counts["nonsensical_review"]})
        if idx%5==0 or idx==len(paths)-1:
            print("JSON_FILE_PROGRESS="+json.dumps({"file":idx+1,"of":len(paths),
                   "reviewed":valid_reviews,"coins":len(bars)}),flush=True)
        del blob
        gc.collect()
    btc=bars.get("BTC-USDT",{})
    # How many 5m bars and 2.5h-continuous spans are actually saved?
    counts=Counter()
    counted=0
    candidates=[]
    seen=0
    for coin,rows in bars.items():
        for ts in sorted(rows):
            b=rows[ts]
            if side(b)!="RED":continue
            counts["red_bars"]+=1
            old=rows.get(ts-F)
            if old is None or side(old)!="GREEN":continue
            counts["red_after_green"]+=1
            if old[4]<=0 or not (3<=b[4]/old[4]<4):continue
            counts["volume_3to4x"]+=1
            prev=[rows.get(ts-i*F) for i in range(1,11)]
            if any(x is None for x in prev):counts["missing_avg10_history"]+=1;continue
            m10=sum(x[4] for x in prev)/10
            if m10<=0 or b[4]>m10:continue
            counts["below_mean10"]+=1
            earlier=rows.get(ts-30*F)
            if earlier is None:
                counts["missing_150m_close"]+=1;continue
            if pct(b[3],earlier[3])<0:continue
            counts["prior_150min_up"]+=1
            change15=calc15(rows,ts)
            if change15 is None:counts["missing_completed15m"]+=1;continue
            if change15<-.10322961:continue
            counts["coin15m_ok"]+=1
            if b[1]<=old[1] and b[2]>=old[2]:continue
            counts["not_inside_bar"]+=1
            btc0=btc.get(ts);btc3=btc.get(ts-3*F)
            if btc0 is None or btc3 is None:
                counts["missing_btc15"]+=1;continue
            bp=pct(btc0[3],btc3[3])
            if bp>.062055813:continue
            counts["btc15_ok"]+=1
            rank=ranks.get(ts,{}).get(coin)  # rank scan at close ts+F -> stored ts
            if rank is None:counts["missing_valid_asof_rank"]+=1;continue
            if rank>200:continue
            counts["rank_ok"]+=1
            future=rows.get(ts+F)
            if future is None:
                counts["missing_entry_open"]+=1;continue
            candidates.append({"coin":coin,"signal_ms":ts,"entry_ms":ts+F,
                   "entry_price":future[0],"rank":rank,
                   "btc_15_pct":bp,"coin_15_pct":change15})
    # Only complete closed trade outcomes in saved review candles; missing next candle => unresolved.
    complete=[]
    incomplete=[]
    for t in candidates:
        coin=t["coin"];r=bars[coin];ts=t["signal_ms"]
        red_v=r[ts][4]
        entry=t["entry_ms"];entry_px=t["entry_price"]
        why=None
        for step in range(1,1000):
            curts=ts+step*F
            b=r.get(curts)
            if b is None:
                why="missing_5m_review_during_trade";break
            history=[r.get(curts-i*F) for i in range(1,11)]
            if any(z is None for z in history):
                why="missing_mean10_during_trade";break
            m10=sum(z[4] for z in history)/10
            if m10<=0:why="zero_volume_mean";break
            if b[4]<=m10:
                col=side(b)
                if col=="RED":red_v=b[4]
                if col=="GREEN" and b[4]>red_v:
                    exitb=r.get(curts+F)
                    if exitb is None:
                        why="missing_exit_next_open";break
                    gross=100*(1-exitb[0]/entry_px)
                    complete.append({**t,"exit_ms":curts+F,
                       "gross_pct":round(gross,6),
                       "net_pct_est":round(gross-ROUNDTRIP_FEE,6),
                       "result":"WIN" if gross>0 else "LOSS" if gross<0 else "FLAT"})
                    why="completed"
                    break
        if why!="completed":incomplete.append({**t,"reason":why or "not_closed_within_1000_5m_bars"})
    wc=Counter(t["result"] for t in complete)
    gross=sum(t["gross_pct"] for t in complete)
    net=sum(t["net_pct_est"] for t in complete)
    report={
        "source":"ONLY saved GitHub JSON log next_candle_review+indicators_after_5m, never external candles",
        "files_scanned":len(paths),
        "range_start_uk":min((p["start_uk"] for p in file_stats if p["start_uk"]),default=None),
        "range_end_uk":max((p["end_uk"] for p in file_stats if p["end_uk"]),default=None),
        "saved_review_count":seen_reviews,"usable_5m_reviews":valid_reviews,
        "market_count":len(bars),"BTC_bars_saved":len(btc),
        "stale_candle_snapshot_records_ignored":stale,
        "conflicting_duplicate_candle_reviews":conflicts,
        "saved_asof_rank_timestamps":len(ranks),
        "progressive_filter_counts":dict(counts),
        "strict_signal_count":len(candidates),
        "closed_trades":len(complete),"wins":wc["WIN"],"losses":wc["LOSS"],"flat":wc["FLAT"],
        "still_unresolved_due_to_gaps":len(incomplete),
        "success_pct_if_decided":round(100*wc["WIN"]/(wc["WIN"]+wc["LOSS"]),2) if wc["WIN"]+wc["LOSS"] else None,
        "gross_sum_percentage_points":round(gross,6),
        "estimated_net_sum_percentage_points":round(net,6),
        "estimated_net_usdt_at_20_per_trade":round(20*net/100,6),
        "closed_trades_detail":[{"coin":t["coin"],"entry_uk":uk(t["entry_ms"]),
              "exit_uk":uk(t["exit_ms"]),"rank":t["rank"],
              "gross_pct":t["gross_pct"],"net_pct_est":t["net_pct_est"],
              "result":t["result"]} for t in complete],
        "unresolved_trades_detail":[{"coin":t["coin"],"entry_uk":uk(t["entry_ms"]),"reason":t["reason"]} for t in incomplete],
        "per_file_coverage":file_stats,
        "limitations":"Only coins and candles recorded in scan JSON reviews. Archive logs TOP7/TOP100, not full TOP200; missing BTC bars/coin history not backfilled from elsewhere. Missing future bars are unresolved, never marked as wins or losses. Exact rule uses rank observed at same 5m signal close from non-stale per-coin snapshot; many snapshots are stale and excluded. 0.12% fee estimate, no spread or slippage. Independent simultaneous coin positions are not executable with one 20 USDT stake. Data-mined rule 20/0 on prior day is not predictive evidence."
    }
    print("JSON_BACKTEST_RESULT="+json.dumps({k:v for k,v in report.items()
       if k not in ("closed_trades_detail","unresolved_trades_detail","per_file_coverage")},
       ensure_ascii=False,separators=(",",":")),flush=True)
    print("JSON_BACKTEST_TRADES="+json.dumps(report["closed_trades_detail"][:100],
            ensure_ascii=False,separators=(",",":")),flush=True)
    p=Path("research_saved_json_short20_replay_results.json")
    p.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
if __name__=="__main__":main()
