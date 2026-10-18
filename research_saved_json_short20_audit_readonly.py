"""Read-only audit of saved bot JSON schemas for exact user-requested 20/0 SHORT rule.
No network or trading, no file writes, no printing secrets, identifiers or balances.
"""
import json,collections
from pathlib import Path
def obdesc(v,depth=0):
    if isinstance(v,dict):
        return {"kind":"object","keys":list(v)[:80],"sample_lists":{k:len(x) for k,x in v.items() if isinstance(x,list)}}
    if isinstance(v,list):
        return {"kind":"list","length":len(v),"first_type":type(v[0]).__name__ if v else None,
                "first_keys":list(v[0])[:80] if v and isinstance(v[0],dict) else [],
                "last_keys":list(v[-1])[:80] if v and isinstance(v[-1],dict) else []}
    return {"kind":type(v).__name__}
def scan(path):
    data=json.loads(path.read_text(encoding="utf-8"))
    result={"name":str(path),"size":path.stat().st_size,"root":obdesc(data)}
    if isinstance(data,dict):
        interesting=[k for k in data if ("hist" in k.lower() or "trade" in k.lower() or
                "entry" in k.lower() or "signal" in k.lower() or
                "candl" in k.lower() or "market" in k.lower() or
                "top" in k.lower() or "review" in k.lower() or "feature" in k.lower() or "observ" in k.lower())]
        result["relevant_nested"]={k:obdesc(data[k]) for k in interesting[:30]}
        for k in ["trade_history","history","events","observations","trades","entries","learning","records","scans"]:
            ar=data.get(k)
            if isinstance(ar,list) and ar:
                result["sample_keys_"+k]=sorted({key for item in ar[:min(30,len(ar))] if isinstance(item,dict) for key in item})[:150]
                result["sample_nested_"+k]={j:obdesc(ar[0].get(j)) for j in list(ar[0])[:30] if isinstance(ar[0].get(j),(dict,list))}
                result["last_entry_ms_"+k]=int(ar[-1].get("entry_ms") or ar[-1].get("opened_ms") or ar[-1].get("signal_close_ms") or 0) if isinstance(ar[-1],dict) else None
        if "trade_history" in data:
            t=data["trade_history"]
            result["history_summary"]={"length":len(t),"side_counts":dict(collections.Counter(str(x.get("side")) for x in t)),
              "outcome_counts":dict(collections.Counter(str(x.get("result")) for x in t)),
              "with_snapshot":sum(bool(x.get("entry_indicators") or x.get("entry_snapshot") or x.get("signal_snapshot")) for x in t),
              "last_3_field_types":[{k:type(v).__name__ for k,v in x.items()} for x in t[-3:]]}
    if isinstance(data,list) and data and isinstance(data[0],dict):
        result["sample0_fields"]={k:obdesc(v) for k,v in data[0].items() if isinstance(v,(dict,list))}
    return result
def main():
    paths=[
    "blofin_live_state.json",
    "blofin_learning_12h.json",
    "blofin_pattern_learning.json",
    "blofin_pending_signal.json",
    "strategy_watch_history.jsonl",
    "blofin_live_scans/live_watch_history_5m_part0029.json",
    "blofin_live_scans/live_watch_history_5m.json",
    "blofin_live_scans/live_watch_history.json",
    ]
    for p in paths:
        file=Path(p)
        if not file.exists():continue
        if file.suffix==".jsonl":
            a=file.read_text(encoding="utf-8").splitlines()
            obj=json.loads(a[0]) if a else {}
            print("AUDIT_JSON="+json.dumps({"name":p,"lines":len(a),"first":obdesc(obj),"nested":{k:obdesc(v) for k,v in obj.items() if isinstance(v,(list,dict))}},separators=(",",":")),flush=True)
            continue
        try:print("AUDIT_JSON="+json.dumps(scan(file),ensure_ascii=False,separators=(",",":")),flush=True)
        except Exception as ex:print("AUDIT_ERROR="+json.dumps({"name":p,"error":type(ex).__name__+": "+str(ex)[:180]}),flush=True)
if __name__=="__main__":main()
