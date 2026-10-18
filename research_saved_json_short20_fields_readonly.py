"""Read-only compact field audit without secret values; debug replay feasibility."""
import json,collections
from pathlib import Path
def shape(x):
    if isinstance(x,dict):return {k:shape(v) for k,v in x.items() if k not in ("history_id","code_commit","chart_path","message") and len(str(k))<70}
    if isinstance(x,list):return {"length":len(x),"first":shape(x[0]) if x else None}
    if isinstance(x,(int,float,bool)):return x
    if isinstance(x,str):return x[:120] if len(x)<120 else "<long text>"
    return None
def main():
    state=json.loads(Path("blofin_live_state.json").read_text())
    hist=state["trade_history"]
    print("TRADE_SNAPSHOTS="+json.dumps([
      {"side":t.get("side"),"result":t.get("result"),"opened_ms":t.get("opened_ms"),
       "snapshot":shape(t.get("signal_snapshot"))}
      for t in hist[-30:] if t.get("side")=="SHORT"
    ][:4],ensure_ascii=False,separators=(",",":")),flush=True)
    learning=json.loads(Path("blofin_learning_12h.json").read_text())
    print("LEARNING_INFO="+json.dumps({"scans":len(learning.get("scans",[])),
         "last_capture_ms":learning.get("last_capture_ms"),
         "first_instrument":shape(learning["scans"][0]["instruments"][0]),
         "last_instrument":shape(learning["scans"][-1]["instruments"][0])},ensure_ascii=False,separators=(",",":")),flush=True)
    path=Path("blofin_live_scans/live_watch_history_5m_part0029.json")
    data=json.loads(path.read_text())
    scan=data["scans"]
    print("SCAN_RANGE="+json.dumps({"scans":len(scan),"first_ms":scan[0].get("signal_close_ms"),
           "last_ms":scan[-1].get("signal_close_ms"),
           "first_instruments":len(scan[0].get("instruments",[])),
           "last_instruments":len(scan[-1].get("instruments",[])),
           "instrument_sample":shape(scan[-1]["instruments"][0])},ensure_ascii=False,separators=(",",":")),flush=True)
    for s in scan[-3:]:
        for it in s["instruments"][:3]:
            c=it.get("candle") or {}
            print("SCAN_CANDLE_EXAMPLE="+json.dumps({"time":s.get("signal_close_ms"),"coin":it.get("inst"),
              "candle":c,"short":it.get("short"),"review":it.get("next_candle_review")},separators=(",",":"),ensure_ascii=False)[:7000],flush=True)
            break
if __name__=="__main__":main()
