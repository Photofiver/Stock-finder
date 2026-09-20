import json
import blofin_live_hourly as bot

STATE="blofin_live_state.json"
OUT="test1_macd_analysis_result.json"
START=1789914169376
END=None

with open(STATE,"r",encoding="utf-8") as f:
    st=json.load(f)
END=int((st.get("test1_reversed") or {}).get("started_at_ms") or 10**18)
trades=[t for t in st.get("trade_history",[]) if START <= int(t.get("opened_ms") or 0) < END]
trades=trades[:30]

def ema(vals,n):
    out=[None]*len(vals)
    if len(vals)<n:
        return out
    seed=sum(vals[:n])/n
    out[n-1]=seed
    a=2/(n+1)
    prev=seed
    for i in range(n,len(vals)):
        prev=a*vals[i]+(1-a)*prev
        out[i]=prev
    return out

def macd_states(bars):
    c=[x["c"] for x in bars]
    e12=ema(c,12); e26=ema(c,26)
    m=[None]*len(bars); vals=[]; idx=[]
    for i in range(len(bars)):
        if e12[i] is not None and e26[i] is not None:
            m[i]=e12[i]-e26[i]; vals.append(m[i]); idx.append(i)
    sigv=ema(vals,9)
    s=[None]*len(bars)
    for k,i in enumerate(idx):
        if k < len(sigv):
            s[i]=sigv[k]
    out=[None]*len(bars)
    hist=[None]*len(bars)
    for i in range(len(bars)):
        if m[i] is not None and s[i] is not None:
            hist[i]=m[i]-s[i]
            if m[i]>s[i]: out[i]="LONG"
            elif m[i]<s[i]: out[i]="SHORT"
    return out,hist,m,s

rows=[]
for n,t in enumerate(trades,1):
    inst=t["inst"]; side=t["side"]; opened=int(t["opened_ms"])
    close_ms=(opened//300000)*300000
    raw=bot.market_get("/api/v1/market/candles",{
        "instId":inst,"bar":"5m","after":str(close_ms+300000),"limit":"120"
    })
    bars=bot.parse_candles(raw)
    bars=[b for b in bars if int(b["ts"])+300000 <= close_ms]
    bars=sorted(bars,key=lambda x:x["ts"])
    states,hist,macd,signal=macd_states(bars)
    state=states[-1] if bars and states else None
    hv=hist[-1] if bars and hist else None
    allowed=(state==side)
    rows.append({
        "n":n,"inst":inst,"side":side,"opened_ms":opened,"signal_close_ms":close_ms,
        "net_pnl_usdt":t.get("net_pnl_usdt"),"macd_state":state,
        "macd_hist":hv,"would_enter":allowed
    })

entered=[r for r in rows if r["would_enter"]]
wins=[r for r in entered if float(r["net_pnl_usdt"] or 0)>0]
losses=[r for r in entered if float(r["net_pnl_usdt"] or 0)<0]
result={
    "total_test1_trades":len(rows),
    "would_enter_with_macd_filter":len(entered),
    "filtered_out":len(rows)-len(entered),
    "entered_wins_original_outcome":len(wins),
    "entered_losses_original_outcome":len(losses),
    "rows":rows
}
with open(OUT,"w",encoding="utf-8") as f:
    json.dump(result,f,ensure_ascii=False,indent=2)
print(json.dumps(result,ensure_ascii=False))
