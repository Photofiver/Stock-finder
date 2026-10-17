"""READ ONLY: TRULY NEW Oct17 UK full-day forward validation of 5 frozen setups.

FROZEN before any 17 October full-day trade outcomes were read. These five
settings came from the Oct1-16 study (four candidates) plus the earlier
Oct9-16 discovery (one candidate). Backtests never alter LIVE or place orders.
Uses full 390-market public OHLC 5m candles, causal TOP200 ranking,
three predetermined 8-hour windows, and 0.22% assumed round-trip costs.
"""
import gzip
import json
from datetime import datetime,timedelta
from pathlib import Path

import blofin_top200_new_volume_reversals_readonly as rev

ASOF="2026-10-18"
T0=datetime(2026,10,10,tzinfo=rev.UK)
T1=datetime(2026,10,18,tzinfo=rev.UK)
rev.START=int(T0.timestamp()*1000)
rev.END=int(T1.timestamp()*1000)

# Freeze all choices. Do not choose on Oct17 result.
frozen=[
 ("BREAKOUT_FADE",4,.2,-1,"TOP_RANK",(.03,.02,24)),
 ("EXHAUST3_FADE",2.5,.5,-1,"HIGHEST_BODY",(.03,.01,24)),
 ("BREAKOUT_FADE",1.5,.5,-1,"HIGHEST_BODY",(.03,.02,24)),
 ("PRICE_SWEEP_REVERSE",2.5,.5,-1,"HIGHEST_BODY",(.02,.01,12)),
 ("BREAKOUT_FADE",2.5,.5,-1,"HIGHEST_VOL_RATIO",(.03,.01,24)),
]
symbols=[]
missing={}
markets={}
for shard in range(4):
    with gzip.open(Path("shards")/f"shard_{shard}.json.gz","rt",encoding="utf-8") as f:
        payload=json.load(f)
    if payload.get("asof_uk")!=ASOF:
        raise RuntimeError("Shards not of correct period")
    symbols.extend(payload["symbols_in_shard"])
    missing.update(payload.get("errors",{}))
    for inst,d in payload["candle_rows"].items():
        if inst in markets:raise RuntimeError("Duplicate")
        markets[inst]={int(k):tuple(map(float,v)) for k,v in d.items()}
print("FORWARD17_MARKET_COVERAGE="+str(len(markets))+" ELIGIBLE="+str(len(set(symbols)))+" MISSING="+str(len(missing)),flush=True)
if len(markets)<350:raise RuntimeError("Market coverage too low")
rev.DATA=markets
rev.EVENTS,counts=rev.events_from_history(markets)
result=[]
for fam,vol,body,side,selector,x in frozen:
    assert x in rev.EXITS
    cfg=(fam,vol,body,side,selector,rev.EXITS.index(x))
    day=rev.eval_config(cfg,["2026-10-17"],need_detailed=True)
    stat=rev.calc_score(day)
    result.append({"rule":{"family":fam,"vol_median20_min":vol,
                           "body_min_pct":body,"direction":"SHORT",
                           "choose_equal_time":selector,
                           "tp_pct":x[0]*100,"sl_pct":x[1]*100,
                           "max_hold_minutes":x[2]*5},
                   "forward_17oct":stat})
report={
    "asof":"2026-10-18",
    "fresh_day_uk":"2026-10-17",
    "symbols_eligible":len(set(symbols)),"symbols_with_history":len(markets),
    "missing":missing,
    "frozen_before_forward_test":True,
    "rules_evaluated":len(frozen),
    "results":result,
    "profit_goal":"+0.60 USDT NET on 20USDT exposure for 3 non-overlapping daily setups",
    "caveats":"Current listed instruments only; forward is historical day not live PAPER, full order-book liquidity and funding ignored; OHLC stop-first on same candle, assumes 0.22% fees/slippage. Cannot guarantee 3% daily. LIVE was never altered."
}
Path("blofin_top200_forward_17oct_frozen_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("FROZEN_FORWARD_OCT17_RESULT="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
