"""READ ONLY profit of the chosen 20/0 in-sample SHORT entry signals.
Use only the previously selected 37 wins and 26 losses.
"""
from collections import Counter
import json
from pathlib import Path
import short60_volume_mean10_24hour_readonly as core
from short37_extra_indicators_readonly import features

FEE_PCT=0.12
def main():
    data,errors,total=core.load()
    if errors or len(data)!=390:raise RuntimeError("Incomplete archived candles")
    ranks,_=core.rankings(data)
    res=core.test_period(data,ranks,core.START,core.END,1.6)
    if (res["wins"],res["losses"])!=(528,649):
        raise RuntimeError("Underlying 24h test not reproduced")
    selected=[]
    for trade in res["trades"]:
        if trade["outcome"] not in ("WIN","LOSS"):continue
        if trade["volume_ratio"] is None or not 3<=trade["volume_ratio"]<4:continue
        f=features(trade,data[trade["inst"]],data["BTC-USDT"])
        if f is None:raise RuntimeError("Missing entry feature")
        if f["price_prior150_pct"]>=0:selected.append((trade,f))
    n=Counter(t["outcome"] for t,f in selected)
    if (n["WIN"],n["LOSS"])!=(37,26):
        raise RuntimeError("Wrong 37/26 selection: "+repr(n))
    hits=[(t,f) for t,f in selected
       if f["btc_prior15_pct"] is not None
       and f["btc_prior15_pct"]<=0.062055813
       and f["completed_15m_change_pct"] is not None
       and f["completed_15m_change_pct"]>=-0.10322961
       and f["inside_bar"]<=0]
    cnt=Counter(t["outcome"] for t,f in hits)
    if (cnt["WIN"],cnt["LOSS"])!=(20,0):
        raise RuntimeError("Mismatch to earlier 20/0: "+repr(cnt))
    gross=sum(t["gross_pct"] for t,f in hits)
    net=gross-FEE_PCT*len(hits)
    report={
      "period_uk":"2026-10-17 18:00 to 2026-10-18 18:00",
      "selection":"Only original selected 37 winning and 26 losing SHORT trades, rule BTC last 15m <= +0.062055813%, completed 15m >= -0.10322961%, signal candle not inside previous.",
      "completed":len(hits),
      "winners":cnt["WIN"],
      "losers":cnt["LOSS"],
      "gross_sum_percentage_points":round(gross,6),
      "estimated_fee_sum_percentage_points":round(FEE_PCT*len(hits),6),
      "estimated_net_sum_percentage_points":round(net,6),
      "average_gross_per_trade_pct":round(gross/len(hits),6),
      "average_net_per_trade_pct":round(net/len(hits),6),
      "estimated_net_usdt_flat_20_each":round(20*net/100,6),
      "hypothetical_daily_return_pct_if_all_20_positions_reinvested_from_20usdt_capital_serially":None,
      "trades":[{"coin":t["inst"],"entry_uk":core.clock(t["entry_ms"]),"exit_uk":core.clock(t["exit_ms"]),"gross_pct":t["gross_pct"],"net_est_pct":round(t["gross_pct"]-FEE_PCT,6)} for t,f in hits],
      "warning":"Sum of percentages across independent potentially overlapping coins is NOT a percent daily portfolio return. Cannot assume all 20 can execute serially on one 20 USDT account. Fees assumed 0.12% roundtrip, no slippage/spread/funding. Strong selection bias, 20/0 fit retrospectively."
    }
    Path("short_20wins_0losses_profit_20261018.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("EXACT_20_WIN_PROFIT="+json.dumps({k:v for k,v in report.items() if k!="trades"},ensure_ascii=False,separators=(",",":")),flush=True)
if __name__=="__main__":main()
