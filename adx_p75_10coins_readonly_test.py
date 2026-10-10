"""One-off read-only 10 real BloFin coin test; NO orders/credentials, NO fees."""
import concurrent.futures, json, math, time
import requests
from urllib.parse import urlencode
# Use the same HTTP client and User-Agent as the working LIVE BloFin scanner.
from datetime import datetime, timezone

COINS = "STRK CHIP TIA AERO AZTEC CAP NEAR MAGIC BAT PIXEL C98 OP WLD ZK BTC".split()
FIVE, TEN = 300000, 600000

def prices(inst):
    data = {}
    cursor = None
    for page in range(2):
        args = {"instId":inst+"-USDT","bar":"5m","limit":"1440"}
        if cursor is not None: args["after"]=str(cursor)
        url = "https://openapi.blofin.com/api/v1/market/candles?" + urlencode(args)
        for attempt in range(3):
            try:
                response_http=requests.get(
                    url, headers={"Accept":"application/json",
                                  "User-Agent":"Mozilla/5.0 BloFinStockFinder/1.0"},
                    timeout=25,
                )
                response_http.raise_for_status()
                response=response_http.json()
                if str(response.get("code"))!="0":raise ValueError(str(response)[:250])
                rows=response.get("data",[])
                break
            except Exception:
                if attempt==2:raise
                time.sleep(attempt+1)
        if not rows:break
        for r in rows:
            if len(r)>=9 and str(r[8])!="1":continue
            t=int(r[0]);data[t]=(t,float(r[1]),float(r[2]),float(r[3]),float(r[4]))
        earliest=min(int(x[0]) for x in rows)
        if cursor is not None and earliest>=cursor:break
        cursor=earliest
        if len(rows)<1440:break
    grouped={}
    for r in data.values():grouped.setdefault(r[0]//TEN*TEN,[]).append(r)
    series=[]; tail=[]
    for t, group in sorted(grouped.items()):
        group.sort()
        if len(group)!=2 or group[0][0]!=t or group[1][0]!=t+FIVE:continue
        o=(t,group[0][1],max(x[2] for x in group),min(x[3] for x in group),group[-1][4])
        if tail and t-tail[-1][0]!=TEN:tail=[]
        tail.append(o)
        if len(tail)>len(series):series=tail[:]
    return series,len(data)

def wilder_adx(b):
    p=14; out=[None]*len(b)
    if len(b)<29:return out
    tr=[];plus=[];minus=[]
    for k in range(1,len(b)):
        up=b[k][2]-b[k-1][2];down=b[k-1][3]-b[k][3]
        plus.append(up if up>down and up>0 else 0.)
        minus.append(down if down>up and down>0 else 0.)
        tr.append(max(b[k][2]-b[k][3],abs(b[k][2]-b[k-1][4]),abs(b[k][3]-b[k-1][4])))
    a=sum(tr[:p]);c=sum(plus[:p]);d=sum(minus[:p]);dx=[]
    for j in range(p-1,len(tr)):
        if j>=p:a=a-a/p+tr[j];c=c-c/p+plus[j];d=d-d/p+minus[j]
        pp=100*c/a if a else 0.;mm=100*d/a if a else 0.
        dx.append(100*abs(pp-mm)/(pp+mm) if pp+mm else 0.)
        if len(dx)>=p:
            out[j+1]=sum(dx[:p])/p if len(dx)==p else (out[j]*(p-1)+dx[-1])/p
    return out

def percentile75(values):
    s=sorted(values);u=(len(s)-1)*.75;k=int(u)
    return s[k]+(u-k)*(s[k+1]-s[k]) if k+1<len(s) else s[k]

def signals(b,adx):
    cl=[r[4] for r in b];ans={}
    for i in range(230,len(b)-1):
        prior=adx[i-200:i]
        if adx[i] is None or len(prior)!=200 or any(v is None for v in prior):continue
        was10=sum(cl[i-10:i])/10;was20=sum(cl[i-20:i])/20
        now10=sum(cl[i-9:i+1])/10;now20=sum(cl[i-19:i+1])/20
        side="LONG" if was10<=was20 and now10>now20 else "SHORT" if was10>=was20 and now10<now20 else None
        if side:ans[i+1]=(side,adx[i],percentile75(prior))
    return ans

def simulate(b,sign,mode):
    position=None;trades=[];amb=0;skipped=0
    for i in range(231,len(b)):
        _,o,h,l,c=b[i]
        stop=sum(r[4] for r in b[i-9:i])/9
        if position is None and i in sign:
            side,adx,limit=sign[i]
            ok=(mode=="NO_ADX" or (mode=="ADX38" and adx<38) or (mode=="P75" and adx<limit))
            if ok:
                if (side=="LONG" and o<=stop) or (side=="SHORT" and o>=stop):skipped+=1
                else:position=(side,o,i)
        if position is None:continue
        side,entry,entry_index=position
        tp=entry*(1.01 if side=="LONG" else .99)
        touch_tp=h>=tp if side=="LONG" else l<=tp
        touch_sl=l<=stop if side=="LONG" else h>=stop
        stop_at_open=o<=stop if side=="LONG" else o>=stop
        tp_at_open=o>=tp if side=="LONG" else o<=tp
        px=None;reason=None
        if stop_at_open:px=o;reason="SL_GAP"
        elif tp_at_open:px=o;reason="TP_GAP"
        elif touch_sl and touch_tp:px=stop;reason="SL_AMBIGUOUS";amb+=1
        elif touch_sl:px=stop;reason="SL"
        elif touch_tp:px=tp;reason="TP"
        if px is not None:
            pnl=(px/entry-1)*100*(1 if side=="LONG" else -1)
            trades.append({"side":side,"pnl":pnl,"entry_index":entry_index,"exit_index":i,"reason":reason})
            position=None
    return {"n":len(trades),"wins":sum(t["pnl"]>0 for t in trades),
            "losses":sum(t["pnl"]<0 for t in trades),
            "sum_pct":round(sum(t["pnl"] for t in trades),4),
            "ambiguous_as_sl":amb,"skipped_bad_side":skipped,
            "unclosed_position":position is not None}

def inspect(coin):
    try:
        bars,n5=prices(coin)
        if len(bars)<300:return {"coin":coin,"status":"INSUFFICIENT","bars_5m":n5,"bars_10m":len(bars)}
        adx=wilder_adx(bars);s=signals(bars,adx)
        methods={m:simulate(bars,s,m) for m in ("NO_ADX","ADX38","P75")}
        return {"_backtest_bars":bars,"_signals":s,"coin":coin,"status":"OK","bars_5m":n5,"bars_10m":len(bars),
                "date_start":datetime.fromtimestamp(bars[0][0]/1000,timezone.utc).isoformat(),
                "date_end":datetime.fromtimestamp(bars[-1][0]/1000,timezone.utc).isoformat(),
                "cross_signals":len(s),
                "latest_p75":round(list(s.values())[-1][2],2) if s else None,
                "latest_adx":round(list(s.values())[-1][1],2) if s else None,
                "methods":methods}
    except Exception as ex:return {"coin":coin,"status":"ERROR","error":str(ex)[:400]}

def one_position_portfolio(data, mode, initial_usdt=10.0):
    """One concurrent position, full 10 USDT balance per trade, 1x, zero fees.
    Rank by the 24h momentum of ONLY these ten assets, then select among TOP7.
    This cannot reproduce the actual BloFin TOP7 of the entire exchange.
    A signal is from the preceding closed candle; fill at next 10m open.
    """
    books={}
    all_times=set()
    for coin,(b,sign) in data.items():
        idx={row[0]:i for i,row in enumerate(b)}
        books[coin]={"bars":b,"signals":sign,"idx":idx}
        all_times.update(b[i][0] for i in range(231,len(b)))
    equity=float(initial_usdt);pos=None;trades=[];skip_stop=0
    skipped_when_busy=0;daily={};ambiguous=0;dates={}
    count_candidates=0
    for t in sorted(all_times):
        ranked=[]
        for coin,item in books.items():
            i=item["idx"].get(t)
            if i is None or i<231:continue
            b=item["bars"]
            past=b[i-1][4]
            prev=b[i-145][4]
            if prev>0:ranked.append((past/prev-1,coin))
        ranked.sort(key=lambda x:(-x[0],x[1]))
        top7=[x[1] for x in ranked[:7]]
        eligible=[]
        for coin in top7:
            item=books[coin];i=item["idx"].get(t)
            if i not in item["signals"]:continue
            side,adx,limit=item["signals"][i]
            if mode=="ADX38" and not (adx<38):continue
            if mode=="P75" and not (adx<limit):continue
            count_candidates+=1
            eligible.append((coin,side,i,adx))
        if pos is None:
            for coin,side,i,adx in eligible:
                b=books[coin]["bars"];o=b[i][1]
                stop=sum(x[4] for x in b[i-9:i])/9
                if (side=="LONG" and o<=stop) or (side=="SHORT" and o>=stop):
                    skip_stop+=1
                    continue
                pos={"coin":coin,"side":side,"entry":o,"entry_ts":t,"entry_index":i,"adx14":adx}
                break
        else:
            skipped_when_busy+=len(eligible)
        if pos is None:continue
        item=books[pos["coin"]]
        i=item["idx"].get(t)
        if i is None:continue
        b=item["bars"];row=b[i]
        _,o,h,l,c=row
        side=pos["side"];entry=pos["entry"]
        stop=sum(x[4] for x in b[i-9:i])/9
        tp=entry*(1.01 if side=="LONG" else .99)
        hit_tp=h>=tp if side=="LONG" else l<=tp
        hit_stop=l<=stop if side=="LONG" else h>=stop
        gap_stop=o<=stop if side=="LONG" else o>=stop
        gap_tp=o>=tp if side=="LONG" else o<=tp
        exitprice=None;reason=None
        if gap_stop:exitprice=o;reason="SL_GAP"
        elif gap_tp:exitprice=o;reason="TP_GAP"
        elif hit_tp and hit_stop:
            exitprice=stop;reason="SL_AMBIGUOUS";ambiguous+=1
        elif hit_stop:exitprice=stop;reason="SL"
        elif hit_tp:exitprice=tp;reason="TP"
        if exitprice is None:continue
        growth=(exitprice/entry-1)*(1 if side=="LONG" else -1)
        equity=equity*(1+growth)
        date=datetime.fromtimestamp(t/1000,timezone.utc).date().isoformat()
        before=daily.get(date,{"open_balance":equity/(1+growth),"pnl_usdt":0,"n":0})
        before["pnl_usdt"]+=equity-equity/(1+growth)
        before["n"]+=1
        daily[date]=before
        trades.append({"coin":pos["coin"],"side":side,"entry_utc":datetime.fromtimestamp(pos["entry_ts"]/1000,timezone.utc).isoformat(),
                       "exit_utc":datetime.fromtimestamp(t/1000,timezone.utc).isoformat(),
                       "entry":entry,"exit":exitprice,"gross_pct":round(growth*100,5),"reason":reason,
                       "adx14":round(pos["adx14"],3)})
        pos=None
    for key in sorted(daily):
        r=daily[key];r["open_balance"]=round(r["open_balance"],6)
        r["pnl_usdt"]=round(r["pnl_usdt"],6)
    return {"mode":mode,"initial_balance_usdt":initial_usdt,
            "final_balance_usdt":round(equity,6),"return_pct":round((equity/initial_usdt-1)*100,4),
            "closed_trades":len(trades),"wins":sum(z["gross_pct"]>0 for z in trades),
            "losses":sum(z["gross_pct"]<0 for z in trades),
            "ambiguous_both_touch_stop_first":ambiguous,"skipped_stop_already_crossed":skip_stop,
            "eligible_while_busy":skipped_when_busy,"total_eligible_signals":count_candidates,
            "open_position_at_end":pos,
            "daily_results_utc":daily,"trade_log":trades}


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        full=list(pool.map(inspect,COINS))
    for row in full:print("FETCH",row["coin"],row["status"],row.get("bars_10m"),row.get("error",""),flush=True)
    selected=[r for r in full if r["status"]=="OK"][:10]
    if len(selected)!=10:raise ValueError("Only "+str(len(selected))+" valid coin histories available; "+str(full))
    books={r["coin"]:(r.pop("_backtest_bars"),r.pop("_signals")) for r in selected}
    chronological={mode:one_position_portfolio(books,mode) for mode in ("NO_ADX","ADX38","P75")}
    totals={}
    for mode in ("NO_ADX","ADX38","P75"):
        totals[mode]={key:round(sum(z["methods"][mode][key] for z in selected),4)
                       for key in ("n","wins","losses","sum_pct","ambiguous_as_sl","skipped_bad_side")}
        totals[mode]["winrate"]=round(100*totals[mode]["wins"]/totals[mode]["n"],2) if totals[mode]["n"] else None
    result={"source":"BloFin real futures 5m aggregated to closed 10m",
            "rules":"MA10/20 signal closed candle; enter next candle open; TP +1%; SL = mean last 9 closed prices; ambiguous intrabar stop-first; zero fees; one trade per coin at time",
            "ADX":"Wilder ADX14, dynamic P75 of EXACTLY 200 prior readings (no future)",
            "ten_coins":selected,"totals":totals,"chronological_single_position":chronological,"unavailable":full[10:]}
    with open("adx_p75_10coins_results.json","w") as f:json.dump(result,f,indent=2)
    print("TOTALS",json.dumps(totals),flush=True)
    for mode,report in chronological.items():
        print("PORTFOLIO",mode,json.dumps({k:v for k,v in report.items() if k!="trade_log"}),flush=True)
    for mode,report in chronological.items():
        tr=report["trade_log"]
        wins=[x for x in tr if x["gross_pct"]>0]
        losses=[x for x in tr if x["gross_pct"]<0]
        reasons={}
        for t in tr:
            k=t["reason"]+"_"+("WIN" if t["gross_pct"]>0 else "LOSS" if t["gross_pct"]<0 else "FLAT")
            reasons[k]=reasons.get(k,0)+1
        print("PNL_BREAKDOWN",mode,json.dumps({
            "winning_trades":len(wins),
            "average_win_pct":round(sum(x["gross_pct"] for x in wins)/len(wins),5) if wins else None,
            "min_win_pct":round(min(x["gross_pct"] for x in wins),5) if wins else None,
            "max_win_pct":round(max(x["gross_pct"] for x in wins),5) if wins else None,
            "winning_sum_pct":round(sum(x["gross_pct"] for x in wins),5),
            "losing_trades":len(losses),
            "average_loss_pct":round(sum(x["gross_pct"] for x in losses)/len(losses),5) if losses else None,
            "min_loss_pct":round(min(x["gross_pct"] for x in losses),5) if losses else None,
            "max_loss_pct":round(max(x["gross_pct"] for x in losses),5) if losses else None,
            "losing_sum_pct":round(sum(x["gross_pct"] for x in losses),5),
            "by_reason":reasons,
        }),flush=True)
    for row in selected:print("COIN",row["coin"],"P75",row["latest_p75"],"sign",row["cross_signals"],"methods",json.dumps(row["methods"]),flush=True)

if __name__=="__main__":main()
