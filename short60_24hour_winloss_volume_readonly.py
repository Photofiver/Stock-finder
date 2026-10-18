"""Read-only volume distribution of 24h SHORT entries: 60% min and mean10 cap.
Reuses the already-completed 390-symbol public BloFin candle shards.
Does not read keys, submit orders, or change LIVE settings/state.
"""
from __future__ import annotations
import json
import math
import statistics
from pathlib import Path
import short60_volume_mean10_24hour_readonly as m

def summary(positions):
    values={}
    for key in ("volume_ratio","volume_mean10_ratio"):
        vs=sorted(float(t[key]) for t in positions if t.get(key) is not None and math.isfinite(float(t[key])))
        values[key]={
            "n":len(vs),
            "median":round(statistics.median(vs),4) if vs else None,
            "average":round(statistics.fmean(vs),4) if vs else None,
            "p10":round(vs[(len(vs)-1)//10],4) if vs else None,
            "p25":round(vs[(len(vs)-1)//4],4) if vs else None,
            "p75":round(vs[3*(len(vs)-1)//4],4) if vs else None,
            "p90":round(vs[9*(len(vs)-1)//10],4) if vs else None,
            "minimum":round(min(vs),4) if vs else None,
            "maximum":round(max(vs),4) if vs else None
        }
    return values

def bins(trades,field,bounds):
    out=[]
    for lo,hi in zip(bounds,bounds[1:]):
        t=[x for x in trades if x.get(field) is not None and lo<=x[field]<hi]
        win=sum(x["outcome"]=="WIN" for x in t)
        loss=sum(x["outcome"]=="LOSS" for x in t)
        flat=sum(x["outcome"]=="FLAT" for x in t)
        out.append({"minimum":lo,"maximum":hi if hi<1e10 else None,
                    "winners":win,"losers":loss,"flat":flat,
                    "success_pct":round(100*win/(win+loss),2) if win+loss else None})
    return out

def main():
    data,errors,total=m.load()
    ranks,coverage=m.rankings(data)
    result=m.test_period(data,ranks,m.START,m.END,1.6)
    assert total==390 and len(data)==390
    if (result["wins"],result["losses"],result["flat"],result["still_open"])!=(528,649,21,34):
        raise RuntimeError("Expected original continuous 24h results 528/649/21/34, found "+
                           repr(tuple(result.get(k) for k in ("wins","losses","flat","still_open"))))
    trades=result["trades"]
    def select(label):return [t for t in trades if t["outcome"]==label]
    desc={label:{"count":len(select(label)),**summary(select(label))}
          for label in ("WIN","LOSS","FLAT")}
    bucket={
      "compared_to_previous_candle":bins(trades,"volume_ratio",[1.6,1.8,2,2.5,3,4,5,7,10,20,100,1e15]),
      "compared_to_mean10":bins(trades,"volume_mean10_ratio",[0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0000001]),
    }
    # Candidate filters, calculated on the same observed trades, not forward-validated.
    filters=[]
    for v in (1.6,1.8,2,2.5,3,4,5):
        subset=[t for t in trades if t.get("volume_ratio") is not None and t["volume_ratio"]>=v]
        w=sum(x["outcome"]=="WIN" for x in subset)
        l=sum(x["outcome"]=="LOSS" for x in subset)
        flat=sum(x["outcome"]=="FLAT" for x in subset)
        filters.append({"min_volume_vs_previous":v,"wins":w,"losses":l,"flat":flat,
                        "winrate_pct":round(100*w/(w+l),2) if w+l else None})
    report={
       "period_uk":[m.clock(m.START),m.clock(m.END)],
       "universe":"390 BloFin currently live crypto USDT perpetual futures, point-in-time TOP200",
       "policy":"SHORT only. Entry volume at least 1.6x previous GREEN candle volume and no greater than prior10 volume mean. Ignore all candles over their own mean10. Exit on valid opposing GREEN volume > last valid RED volume. Entry and exit at NEXT 5m open.",
       "counts":{"wins":result["wins"],"losses":result["losses"],"flat":result["flat"],"still_open":result["still_open"]},
       "statistics":desc,"volume_bins":bucket,"vol_min_sensitivity":filters,
       "explanation":"Volumes are relative ratios only, not absolute units: volume quantity differs by coin. 24h closed position sample. Ratio calculated at entry using prior single opposite-color candle and previous10 volume arithmetic mean.",
       "caveats":"Retrospective category analysis cannot establish causal predictor or guarantee future wins. Multiple parallel symbols, one-time historical sample, cost model absent in category figures. No changes to LIVE.",
    }
    out=Path("short60_24hour_wins_losses_volume_report.json")
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("SHORT60_WIN_LOSS_VOLUME="+json.dumps(report,ensure_ascii=False,separators=(",",":")),flush=True)
if __name__=="__main__":main()
