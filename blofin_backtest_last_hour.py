import requests
from datetime import datetime, timezone, timedelta

BASE = "https://openapi.blofin.com"
TIMEFRAMES = ["4H", "1H", "15m", "5m"]
DURATION_MS = {"4H": 4*60*60*1000, "1H": 60*60*1000, "15m": 15*60*1000, "5m": 5*60*1000, "1m": 60*1000}
TOP_N = 10
NOTIONAL = 10.0
TP_PCT = 0.01
SL_PCT = 0.01


def get(path, params=None):
    r = requests.get(BASE + path, params=params, timeout=20)
    r.raise_for_status()
    p = r.json()
    if str(p.get("code")) != "0":
        raise RuntimeError(p)
    return p.get("data", [])


def parse(raw):
    out=[]
    for row in raw:
        if len(row) < 9 or str(row[8]) != "1":
            continue
        try:
            out.append({
                "ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5])
            })
        except Exception:
            pass
    return sorted(out, key=lambda x:x["ts"])


def top10():
    live=set()
    for x in get("/api/v1/market/instruments"):
        if x.get("state")=="live" and x.get("instType")=="SWAP" and x.get("contractType")=="linear" and x.get("settleCurrency")=="USDT":
            if x.get("instId"): live.add(x["instId"])
    ranked=[]
    for x in get("/api/v1/market/tickers"):
        inst=x.get("instId")
        if inst not in live: continue
        try:
            last=float(x.get("last") or 0); op=float(x.get("open24h") or 0)
            if last>0 and op>0:
                ranked.append(((last/op-1)*100, inst))
        except Exception:
            pass
    ranked.sort(reverse=True)
    return [{"inst":i,"change":ch,"rank":n+1} for n,(ch,i) in enumerate(ranked[:TOP_N])]


def closed_pair(candles, tf, t_ms):
    dur=DURATION_MS[tf]
    eligible=[x for x in candles if x["ts"]+dur <= t_ms]
    if len(eligible)<2: return None
    return eligible[-2], eligible[-1]


def tf_signal(candles, tf, t_ms):
    pair=closed_pair(candles, tf, t_ms)
    if not pair: return None
    prev,cur=pair
    if cur["v"] <= prev["v"]: return None
    if cur["c"] > cur["o"]: return "LONG"
    if cur["c"] < cur["o"]: return "SHORT"
    return None


def price_at_or_before(c1m, t_ms):
    eligible=[x for x in c1m if x["ts"]+60000 <= t_ms]
    return eligible[-1]["c"] if eligible else None


def fmt_ts(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%H:%M")


def main():
    coins=top10()
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    start=now-timedelta(hours=1)
    start_ms=int(start.timestamp()*1000); now_ms=int(now.timestamp()*1000)
    print(f"BACKTEST UTC {start.strftime('%H:%M')}->{now.strftime('%H:%M')} | current TOP10 fixed for test")
    print("TOP10:", ", ".join(f"#{c['rank']} {c['inst']} {c['change']:+.2f}%" for c in coins))

    data={}
    for c in coins:
        inst=c["inst"]
        data[inst]={}
        for tf,limit in [("4H",20),("1H",30),("15m",40),("5m",100),("1m",200)]:
            data[inst][tf]=parse(get("/api/v1/market/candles", {"instId":inst,"bar":tf,"limit":str(limit)}))

    events=[]
    open_pos=None
    trades=[]
    t=start
    while t <= now:
        t_ms=int(t.timestamp()*1000)
        # manage existing position first using the just-closed 1m candle
        if open_pos:
            c1m=data[open_pos["inst"]]["1m"]
            bars=[b for b in c1m if open_pos["entry_t"] < b["ts"]+60000 <= t_ms]
            if bars:
                b=bars[-1]
                if open_pos["side"]=="LONG":
                    hit_sl=b["l"] <= open_pos["sl"]
                    hit_tp=b["h"] >= open_pos["tp"]
                else:
                    hit_sl=b["h"] >= open_pos["sl"]
                    hit_tp=b["l"] <= open_pos["tp"]
                if hit_sl or hit_tp:
                    # Conservative if both levels were touched in the same 1m candle.
                    reason="LOSS" if hit_sl else "WIN"
                    exit_px=open_pos["sl"] if hit_sl else open_pos["tp"]
                    pnl=-NOTIONAL*SL_PCT if hit_sl else NOTIONAL*TP_PCT
                    trades.append({**open_pos,"exit_t":t_ms,"exit":exit_px,"reason":reason,"pnl":pnl})
                    open_pos=None
            if open_pos and t_ms-open_pos["entry_t"] >= 60*60*1000:
                px=price_at_or_before(data[open_pos["inst"]]["1m"], t_ms)
                if px is not None:
                    ret=(px/open_pos["entry"]-1) * (1 if open_pos["side"]=="LONG" else -1)
                    pnl=NOTIONAL*ret
                    trades.append({**open_pos,"exit_t":t_ms,"exit":px,"reason":"TIME","pnl":pnl})
                    open_pos=None

        if open_pos is None:
            candidates=[]
            for c in coins:
                inst=c["inst"]
                sigs={tf:tf_signal(data[inst][tf],tf,t_ms) for tf in TIMEFRAMES}
                lc=sum(1 for s in sigs.values() if s=="LONG")
                sc=sum(1 for s in sigs.values() if s=="SHORT")
                if lc>=3 or sc>=3:
                    side="LONG" if lc>sc else "SHORT"
                    matches=max(lc,sc)
                    px=price_at_or_before(data[inst]["1m"],t_ms)
                    if px:
                        candidates.append((matches,-c["rank"],c,side,sigs,px))
            if candidates:
                candidates.sort(reverse=True, key=lambda x:(x[0],x[1]))
                matches,_,c,side,sigs,px=candidates[0]
                if side=="LONG": tp=px*(1+TP_PCT); sl=px*(1-SL_PCT)
                else: tp=px*(1-TP_PCT); sl=px*(1+SL_PCT)
                open_pos={"inst":c["inst"],"rank":c["rank"],"side":side,"matches":matches,"sigs":sigs,"entry_t":t_ms,"entry":px,"tp":tp,"sl":sl}
                events.append(open_pos.copy())
        t += timedelta(minutes=1)

    print(f"ENTRIES={len(events)} CLOSED={len(trades)} OPEN={1 if open_pos else 0}")
    for i,tr in enumerate(trades,1):
        status=" | ".join(f"{tf}:{tr['sigs'][tf] or '-'}" for tf in TIMEFRAMES)
        print(f"TRADE {i}: {tr['inst']} {tr['side']} {tr['matches']}/4 entry={fmt_ts(tr['entry_t'])} {tr['entry']:.8g} exit={fmt_ts(tr['exit_t'])} {tr['exit']:.8g} {tr['reason']} pnl={tr['pnl']:+.4f} USDT | {status}")
    if open_pos:
        status=" | ".join(f"{tf}:{open_pos['sigs'][tf] or '-'}" for tf in TIMEFRAMES)
        px=price_at_or_before(data[open_pos["inst"]]["1m"],now_ms)
        ret=((px/open_pos["entry"]-1)*(1 if open_pos["side"]=="LONG" else -1)) if px else 0
        print(f"OPEN: {open_pos['inst']} {open_pos['side']} {open_pos['matches']}/4 entry={fmt_ts(open_pos['entry_t'])} {open_pos['entry']:.8g} now={px:.8g} unrealized={NOTIONAL*ret:+.4f} USDT | {status}")
    total=sum(x["pnl"] for x in trades)
    wins=sum(1 for x in trades if x["reason"]=="WIN")
    losses=sum(1 for x in trades if x["reason"]=="LOSS")
    times=sum(1 for x in trades if x["reason"]=="TIME")
    print(f"SUMMARY closed={len(trades)} wins={wins} losses={losses} time={times} gross_pnl={total:+.4f} USDT on 10 USDT notional")
    print("NOTE: ranking uses current TOP10, not historical minute-by-minute TOP10; fees/slippage excluded; if TP and SL hit in same 1m candle, LOSS is assumed.")

if __name__=="__main__":
    main()
