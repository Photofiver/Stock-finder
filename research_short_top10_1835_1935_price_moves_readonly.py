"""Strictly read-only: retrospective SHORT price change for exactly 10 coins
from previously verified BloFin 19:39 snapshot, 18 Oct 2026.
This is a hypothetical same-window entry/exit, NOT volume rule trades.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import blofin_top200_repeat_setup_research as api

UK=ZoneInfo("Europe/London")
DAY="2026-10-18"
COINS=["DGAI-USDT","XMR-USDT","ARC-USDT","IOTX-USDT","SENT-USDT",
       "OPENAI-USDT","BTCDOM-USDT","XDP-USDT","ANTHROPIC-USDT","GWEI-USDT"]
FIVE=300000
START=int(datetime.fromisoformat(DAY+"T18:35:00").replace(tzinfo=UK).timestamp()*1000)
END=int(datetime.fromisoformat(DAY+"T19:35:00").replace(tzinfo=UK).timestamp()*1000)
def get(inst):
    rows,pages=api.candles(inst,END+FIVE,START-FIVE)
    a=rows.get(START);z=rows.get(END-FIVE)
    if not a or not z:return {"coin":inst,"error":"Missing confirmed 5m close"}
    # SHORT from first open to last close, 60 minutes, ideal mid-price fills
    # avoid incorrect approximation using reciprocal (correct short PnL is
    # (entry_price-exit_price)/entry_price)
    short_gross=100*(a[0]-z[3])/a[0]
    long_gross=-short_gross
    return {"coin":inst,"start_open":a[0],"end_close":z[3],
       "hourly_price_change_pct":round(long_gross,6),
       "hypothetical_short_gross_pct":round(short_gross,6),
       "hypothetical_short_net_fee_0p12_pct":round(short_gross-0.12,6),
       "short_outcome_after_fee":"PROFIT" if short_gross>0.12 else "LOSS" if short_gross<0.12 else "BREAK_EVEN",
       "pages":pages}
def main():
    out={}
    with ThreadPoolExecutor(max_workers=5) as pool:
        fs={pool.submit(get,inst):inst for inst in COINS}
        for f in as_completed(fs):
            inst=fs[f]
            try:out[inst]=f.result()
            except Exception as exc:out[inst]={"coin":inst,"error":type(exc).__name__+": "+str(exc)[:150]}
    rows=[out[x] for x in COINS]
    if any("error" in x for x in rows):raise RuntimeError("Some candles unavailable: "+json.dumps(rows))
    summary={"range_uk":[DAY+" 18:35",DAY+" 19:35"],"fixed_top10_from_19_39_uk":COINS,
         "method":"Assume SHORT at exact open of 18:35 5m candle and close at exact close of 19:30 5m candle (19:35 time); these are not actual bot entries.",
         "fees_roundtrip_pct":0.12,
         "profit_after_fees":[x for x in rows if x["short_outcome_after_fee"]=="PROFIT"],
         "loss_after_fees":[x for x in rows if x["short_outcome_after_fee"]=="LOSS"],
         "break_even":[x for x in rows if x["short_outcome_after_fee"]=="BREAK_EVEN"],
         "every_coin":rows,
         "warning":"Selection uses TOP10 as of 19:39, not ranking at 18:35 (hindsight bias); no volume-rule entries, no spread/slippage/funding, these are hypothetical marks."}
    print("SHORT_TOP10_HOUR_COIN_RESULTS="+json.dumps(summary,ensure_ascii=False,separators=(",",":")),flush=True)
    Path("short_top10_1835_1935_prices_only.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
if __name__=="__main__":main()
