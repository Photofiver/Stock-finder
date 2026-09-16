import bisect
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
TIMEFRAMES = ["4H", "1H", "15m", "5m"]
DURATION_MS = {
    "4H": 4 * 60 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "5m": 5 * 60 * 1000,
}
TOP_N = 10
NOTIONAL = 10.0
TP_PCT = 0.01
SL_PCT = 0.01
MAX_HOLD_MS = 60 * 60 * 1000
REQUEST_DELAY = 0.08
MAX_RETRIES = 6


def api_get(path, params=None):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(REQUEST_DELAY)
            r = requests.get(BASE + path, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(max(1.5, 1.5 * (attempt + 1)))
                continue
            r.raise_for_status()
            p = r.json()
            if str(p.get("code")) != "0":
                raise RuntimeError(p)
            return p.get("data", [])
        except Exception as exc:
            last = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


def parse(raw):
    out=[]
    for row in raw:
        if len(row) < 9 or str(row[8]) != "1":
            continue
        try:
            out.append({"ts":int(row[0]),"o":float(row[1]),"h":float(row[2]),"l":float(row[3]),"c":float(row[4]),"v":float(row[5])})
        except Exception:
            pass
    return out


def current_top10():
    live=set()
    for x in api_get("/api/v1/market/instruments"):
        if x.get("state")=="live" and x.get("instType")=="SWAP" and x.get("contractType")=="linear" and x.get("settleCurrency")=="USDT" and x.get("instId"):
            live.add(x["instId"])
    ranked=[]
    for x in api_get("/api/v1/market/tickers"):
        inst=x.get("instId")
        if inst not in live: continue
        try:
            last=float(x.get("last") or 0); op=float(x.get("open24h") or 0)
            if last>0 and op>0: ranked.append(((last/op-1)*100,inst))
        except Exception: pass
    ranked.sort(reverse=True)
    return [{"inst":inst,"change":change,"rank":i+1} for i,(change,inst) in enumerate(ranked[:TOP_N])]


def fetch_recent(inst, bar):
    return sorted(parse(api_get("/api/v1/market/candles", {"instId":inst,"bar":bar,"limit":"1440"})), key=lambda x:x["ts"])


def fmt(ms):
    return datetime.fromtimestamp(ms/1000,tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    coins=current_top10()
    data={}
    for c in coins:
        inst=c["inst"]; data[inst]={}
        for tf in TIMEFRAMES:
            data[inst][tf]=fetch_recent(inst,tf)

    available=[c["inst"] for c in coins if data[c["inst"]]["5m"]]
    starts={i:data[i]["5m"][0]["ts"]+DURATION_MS["5m"] for i in available}
    ends={i:data[i]["5m"][-1]["ts"]+DURATION_MS["5m"] for i in available}
    start=max(min(starts.values()), max((data[i][tf][1]["ts"]+DURATION_MS[tf] for i in available for tf in TIMEFRAMES if len(data[i][tf])>1), default=min(starts.values())))
    # use common window determined by 5m history; signal() safely ignores insufficient older TF data
    start=min(starts.values()); end=max(ends.values())
    close_times={i:{tf:[b["ts"]+DURATION_MS[tf] for b in data[i][tf]] for tf in TIMEFRAMES} for i in available}

    def signal(inst,tf,t):
        bars=data[inst][tf]; times=close_times[inst][tf]
        k=bisect.bisect_right(times,t)-1
        if k<1:return None
        prev,cur=bars[k-1],bars[k]
        if cur["v"]<=prev["v"]:return None
        if cur["c"]>cur["o"]:return "LONG"
        if cur["c"]<cur["o"]:return "SHORT"
        return None

    def bar5(inst,t):
        times=close_times[inst]["5m"]; k=bisect.bisect_right(times,t)-1
        return data[inst]["5m"][k] if k>=0 else None

    trades=[]; entries=0; open_pos=None; t=start; step=DURATION_MS["5m"]
    while t<=end:
        if open_pos:
            b=bar5(open_pos["inst"],t)
            if b and b["ts"]+step>open_pos["entry_t"]:
                if open_pos["side"]=="LONG":
                    hit_sl=b["l"]<=open_pos["sl"]; hit_tp=b["h"]>=open_pos["tp"]
                else:
                    hit_sl=b["h"]>=open_pos["sl"]; hit_tp=b["l"]<=open_pos["tp"]
                if hit_sl or hit_tp:
                    loss=hit_sl
                    trades.append({**open_pos,"reason":"LOSS" if loss else "WIN","pnl":-NOTIONAL*SL_PCT if loss else NOTIONAL*TP_PCT})
                    open_pos=None
            if open_pos and t-open_pos["entry_t"]>=MAX_HOLD_MS:
                b=bar5(open_pos["inst"],t)
                if b:
                    d=1 if open_pos["side"]=="LONG" else -1
                    pnl=NOTIONAL*(b["c"]/open_pos["entry"]-1)*d
                    trades.append({**open_pos,"reason":"TIME","pnl":pnl}); open_pos=None

        if open_pos is None:
            candidates=[]
            for c in coins:
                inst=c["inst"]
                if inst not in available or not(starts[inst]<=t<=ends[inst]):continue
                sigs={tf:signal(inst,tf,t) for tf in TIMEFRAMES}
                longs=sum(v=="LONG" for v in sigs.values()); shorts=sum(v=="SHORT" for v in sigs.values())
                if longs==4 or shorts==4:
                    side="LONG" if longs==4 else "SHORT"
                    b=bar5(inst,t)
                    if b and b["c"]>0:candidates.append((-c["rank"],c,side,b["c"]))
            if candidates:
                candidates.sort(reverse=True,key=lambda x:x[0])
                _,c,side,px=candidates[0]
                open_pos={"inst":c["inst"],"side":side,"entry_t":t,"entry":px,"tp":px*(1.01 if side=="LONG" else 0.99),"sl":px*(0.99 if side=="LONG" else 1.01)}
                entries+=1
        t+=step

    if open_pos:
        b=bar5(open_pos["inst"],end)
        if b:
            d=1 if open_pos["side"]=="LONG" else -1
            trades.append({**open_pos,"reason":"END","pnl":NOTIONAL*(b["c"]/open_pos["entry"]-1)*d})

    wins=sum(x["reason"]=="WIN" for x in trades); losses=sum(x["reason"]=="LOSS" for x in trades)
    times=sum(x["reason"]=="TIME" for x in trades); total=sum(x["pnl"] for x in trades); wl=wins+losses
    print("4OF4 BACKTEST")
    print(f"window_utc={fmt(start)} -> {fmt(end)}")
    print(f"days={(end-start)/86400000:.1f}")
    print(f"entries={entries} closed={len(trades)} wins={wins} losses={losses} time_exits={times}")
    print(f"tp_sl_win_rate={(wins/wl*100 if wl else 0):.2f}% ({wins}/{wl})")
    print(f"gross_pnl={total:+.4f} USDT")
    print("NOTE: current TOP10 fixed historically; fees/slippage excluded; TP/SL checked on 5m bars; both hit in one bar counts as LOSS.")

if __name__=="__main__": main()
