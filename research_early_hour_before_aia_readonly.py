"""READ ONLY: EARLY setup for the hour directly BEFORE AIA's 00:10 UK entry.

Window of candidate entry timestamps: 2026-10-17 23:10 <= T < 2026-10-18
00:10 UK (BST), deliberately excluding AIA 00:10. Historical TOP200 ranked
every completed 5m candle by prior 24h price gain; reset trend volume peak
on UK midnight. Reuse ALL 390 BloFin OHLCV archives from job 39080889854.
No exchange actions and NO modifications to LIVE bot.
"""
from __future__ import annotations
import gzip,json,statistics
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import blofin_top200_repeat_setup_research as m

UK=ZoneInfo("Europe/London")
FIVE=m.FIVE
START=int(datetime(2026,10,17,22,10,tzinfo=timezone.utc).timestamp()*1000)
END=int(datetime(2026,10,17,23,10,tzinfo=timezone.utc).timestamp()*1000)
CACHED_SCAN_START=int(datetime(2026,10,17,22,50,tzinfo=timezone.utc).timestamp()*1000)
OLDEST=CACHED_SCAN_START-330*FIVE
LAST_PUBLIC_CANDLE=int(datetime(2026,10,17,23,50,tzinfo=timezone.utc).timestamp()*1000)
def fmt(t):
    return datetime.fromtimestamp(t/1000,UK).strftime("%Y-%m-%d %H:%M")
rows={};missing={};eligible=set()
for i in range(4):
    with gzip.open(Path("shards")/f"early_hour_{i}.json.gz","rt",encoding="utf-8") as f:
        d=json.load(f)
    if d["shard"]!=i or d["anchor"]!=LAST_PUBLIC_CANDLE:
        raise RuntimeError("Wrong archive")
    eligible.update(d["symbols"])
    missing.update(d["missing"])
    for symbol,raw in d["rows"].items():
        if symbol in rows:raise RuntimeError("Duplicate market "+symbol)
        rows[symbol]={int(t):tuple(v) for t,v in raw.items()}
print("COVERAGE="+json.dumps({"eligible":len(eligible),"usable":len(rows),"missing":missing}),flush=True)
if len(rows)<390 or missing:raise RuntimeError("Require all 390 symbols verified")
# Include the earlier candle at START-5m so that a 23:10 entry signal exists.
times=range(OLDEST-OLDEST%FIVE,LAST_PUBLIC_CANDLE+FIVE,FIVE)
states={name:m.volume_states(candles,times) for name,candles in rows.items()}
events=[];rankcounts=[]
for ts in range(START-FIVE,END-FIVE,FIVE):
    ranks=[]
    for inst,bars in rows.items():
        current=bars.get(ts);prior=bars.get(ts-288*FIVE)
        if current and prior and prior[3]>0:ranks.append((current[3]/prior[3]-1,inst))
    if len(ranks)<200:raise RuntimeError("Incomplete TOP200 at "+fmt(ts))
    rankcounts.append(len(ranks))
    ranks.sort(reverse=True)
    for rank,(_,inst) in enumerate(ranks[:200],1):
        for e in m.calc_event(inst,ts,rows[inst],states[inst],rank):
            if e["family"]!="EARLY" or e["ratio"]<4 or e["body"]<.4:
                continue
            if START<=e["signal_ms"]<END:
                events.append(e)
events.sort(key=lambda e:(e["signal_ms"],e["inst"]))
all_signals=[{"coin":e["inst"],"side":e["side"],"rank":e["rank"],
              "uk":fmt(e["signal_ms"]),"volume_multiple":round(e["ratio"],2),
              "body_pct":round(e["body"],3)} for e in events]
# Pick one globally available trade at a time; use original research's TP/SL
# simulator with 60m/volume-reversal/daily-midnight exit. If missing future data
# for an entry, reserve a position and report exit as unverified.
busy=0;positions=[]
for e in events:
    t=e["signal_ms"]
    if t<busy:continue
    out=m.returns(rows[e["inst"]],states[e["inst"]],e,(.015,.01,12),LAST_PUBLIC_CANDLE+FIVE)
    if out is None:
        busy=t+12*FIVE
        positions.append({"coin":e["inst"],"side":e["side"],"entry_uk":fmt(t),
                          "exit_status":"OPEN_OR_NOT_ENOUGH_FUTURE_DATA","exit_uk":None,"net_pct":None})
    else:
        gross,end,why=out
        busy=end
        positions.append({"coin":e["inst"],"side":e["side"],"entry_uk":fmt(t),
                          "exit_status":why,"exit_uk":fmt(end),
                          "net_pct":round(gross-m.COST_PCT,4)})
data={"window_uk":[fmt(START),fmt(END)],"endpoint_excludes_AIA_0010":True,
      "eligible":len(eligible),"fetched":len(rows),"historical_top200_snapshots":len(rankcounts),
      "signal_count":len(events),"long_signals":sum(e["side"]=="LONG" for e in events),
      "short_signals":sum(e["side"]=="SHORT" for e in events),
      "unique_coins":len(set(e["inst"] for e in events)),
      "one_position_entries":len(positions),"completed_profitable":sum(p["net_pct"] is not None and p["net_pct"]>0 for p in positions),
      "completed_losing":sum(p["net_pct"] is not None and p["net_pct"]<=0 for p in positions),
      "unresolved_positions":sum(p["net_pct"] is None for p in positions),
      "signals":all_signals,"serial_positions":positions,
      "limitations":"Same 390 archived symbol histories as last hourly test; TOP200 reconstructed from 24h rolling candle-close returns. Selection of same-time signals by coin alphabetical. 0.22% trading costs estimated. Test extends beyond original 12:00–18:00 UK session intentionally at user's request. Archives end at 00:50 UK; unresolved positions not called profitable."}
Path("blofin_early_hour_before_aia_report.json").write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
print("EARLY_BEFORE_AIA_COUNTS="+json.dumps({k:v for k,v in data.items() if k not in ("signals","serial_positions","limitations")},separators=(",",":")),flush=True)
print("EARLY_BEFORE_AIA_SIGNALS="+json.dumps(all_signals,separators=(",",":")),flush=True)
print("EARLY_BEFORE_AIA_POSITIONS="+json.dumps(positions,separators=(",",":")),flush=True)
