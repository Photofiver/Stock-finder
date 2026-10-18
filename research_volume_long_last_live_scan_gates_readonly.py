"""READ ONLY: explain exactly which SHORT-to-LONG predicates rejected frozen
last saved TOP200 LIVE scan; public BloFin OHLCV and local state JSON only.
NEVER use private BloFin endpoints. Never change live orders or settings.
"""
import json
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timezone
import statistics,math
import blofin_reverse_short_long_live as rule
import blofin_live_hourly as bot

def main():
    state=json.loads(Path("blofin_live_state.json").read_text())
    last=state.get("volume_long_last_scan") or {}
    close=int(last.get("signal_close_ms") or state.get("last_scan_close_ms") or 0)
    top=list(state.get("last_top200") or [])
    if not close or len(top)!=200:
        raise RuntimeError(f"Missing recorded 200 list or last close (list={len(top)} close={close})")
    btc="BTC-USDT"
    names=list(dict.fromkeys([btc]+top))
    rows={};failures={}
    def get(inst):
        return bot.fetch_signal_bars(inst)
    with ThreadPoolExecutor(max_workers=16) as pool:
        fut={pool.submit(get,n):n for n in names}
        for f in as_completed(fut):
            inst=fut[f]
            try:
                a=f.result()
                if not a:raise ValueError("No OHLCV")
                rows[inst]=rule._by_ts(a,close)
            except Exception as exc:
                failures[inst]=str(exc)[:100]
    btcrows=rows.get(btc,{})
    ts=close-rule.FIVE
    stages=Counter()
    details=[]
    candidates=[]
    bad_price=0
    for rank,inst in enumerate(top,1):
        series=rows.get(inst)
        if not series:
            stages["no_coin_candles"]+=1;continue
        cur=series.get(ts);pr=series.get(ts-rule.FIVE);past=series.get(ts-30*rule.FIVE)
        if not cur or not pr or not past:
            stages["missing_recent_candles_or_150m"]+=1;continue
        if not (pr[3]>pr[0] and cur[3]<cur[0] and pr[4]>0):
            stages["not_red_after_green"]+=1;continue
        stages["red_after_green"]+=1
        ratio=cur[4]/pr[4]
        details.append({"coin":inst,"rank":rank,
             "ratio":round(ratio,5),"red_volume":cur[4],
             "previous_green_volume":pr[4]})
        if not (3<=ratio<4):
            stages["red_after_green_but_wrong_volume_ratio"]+=1;continue
        stages["red_after_green_ratio_3_to_4"]+=1
        mean=rule._previous10_mean(series,ts)
        if mean is None or mean<=0:
            stages["missing_mean10"]+=1;continue
        if cur[4]>mean:
            stages["volume_above_mean10"]+=1;continue
        stages["ratio_and_volume_mean_pass"]+=1
        if 100*(cur[3]/past[3]-1)<0:
            stages["coin_prior150_negative"]+=1;continue
        stages["coin150_pass"]+=1
        c15=rule._completed_coin15_pct(series,ts)
        if c15 is None:stages["missing_coin15"]+=1;continue
        if c15<rule.COIN15_MIN:
            stages["coin_completed15_too_low"]+=1;continue
        stages["coin15_pass"]+=1
        if cur[1]<=pr[1] and cur[2]>=pr[2]:
            stages["red_inside_green"]+=1;continue
        stages["outside_prev_pass"]+=1
        bt=btcrows.get(ts);bo=btcrows.get(ts-3*rule.FIVE)
        if not bt or not bo:stages["missing_btc15"]+=1;continue
        btc15=100*(bt[3]/bo[3]-1)
        if btc15>rule.BTC15_MAX:stages["btc15_too_high"]+=1;continue
        stages["all_pass"]+=1
        candidates.append(inst)
    details.sort(key=lambda d:abs(d["ratio"]-3))
    all_pairs_sorted=sorted(details,key=lambda d:d["ratio"],reverse=True)
    report={
        "close_utc":datetime.fromtimestamp(close/1000,timezone.utc).isoformat(),
        "stored_live_status":last.get("status"),
        "stored_live_scanned":last.get("scanned_confirmed"),
        "stored_live_matched":last.get("matched"),
        "top_n":len(top),
        "public_coins_downloaded":len(rows)-int(btc in rows),
        "missing_or_failed":len(failures),
        "first_failed_gate_counts":dict(stages),
        "closest_to_3x_red_after_green":details[:12],
        "highest_red_after_green_volume_ratios":all_pairs_sorted[:8],
        "full_matches":candidates,
        "note":"Frozen TOP200 list and exact last scan close read from already saved LIVE state. Public BloFin 5m history fetched independently. No private trading APIs. Does not claim whether a candidate would gain or lose."
    }
    print("LIVE_ENTRY_GATE_AUDIT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
    Path("live_volume_long_last_scan_gate_audit.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
if __name__=="__main__":main()
