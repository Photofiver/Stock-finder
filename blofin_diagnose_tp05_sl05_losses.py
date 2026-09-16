import statistics
import time
from datetime import datetime, timezone
import requests

BASE='https://openapi.blofin.com'
SYMBOLS=['ARB-USDT','CNPY-USDT','USELESS-USDT','MARSCOIN-USDT','IOST-USDT','ZEC-USDT','FOLKS-USDT','ZIL-USDT','GRIFFAIN-USDT','H-USDT']
RANK={s:i+1 for i,s in enumerate(SYMBOLS)}
TP_PCT=0.005; SL_PCT=0.005; HOLD_HOURS=2; SPIKE_CAP=2.5
STOCH_K_PERIOD=14; STOCH_K_SMOOTH=3; STOCH_D_PERIOD=3
D1M=60000; D1H=60*D1M
START_TS=int(datetime(2026,9,9,10,0,tzinfo=timezone.utc).timestamp()*1000)
END_TS=int(datetime(2026,9,16,12,0,tzinfo=timezone.utc).timestamp()*1000)
REQUEST_DELAY=0.08; MAX_RETRIES=6

def api_get(path,params=None):
    last=None
    for a in range(MAX_RETRIES):
        try:
            time.sleep(REQUEST_DELAY); r=requests.get(BASE+path,params=params,timeout=30)
            if r.status_code==429: time.sleep(1.5*(a+1)); continue
            r.raise_for_status(); p=r.json()
            if str(p.get('code'))!='0': raise RuntimeError(p)
            return p.get('data',[])
        except Exception as e:
            last=e
            if a<MAX_RETRIES-1: time.sleep(1.5*(a+1))
    raise RuntimeError(last)

def parse(raw):
    out=[]
    for row in raw:
        if len(row)<9 or str(row[8])!='1': continue
        try: out.append({'ts':int(row[0]),'o':float(row[1]),'h':float(row[2]),'l':float(row[3]),'c':float(row[4]),'v':float(row[5])})
        except: pass
    return sorted(out,key=lambda x:x['ts'])

def fetch_1h(inst): return parse(api_get('/api/v1/market/candles',{'instId':inst,'bar':'1H','limit':'1440'}))
def fetch_1m(inst,entry_t):
    end=entry_t+HOLD_HOURS*D1H
    bars=parse(api_get('/api/v1/market/candles',{'instId':inst,'bar':'1m','after':str(end),'limit':'180'}))
    return [b for b in bars if entry_t+D1M<=b['ts']<end]
def sma(vals,p):
    out=[None]*len(vals)
    for i in range(p-1,len(vals)):
        w=vals[i-p+1:i+1]
        if any(v is None for v in w): continue
        out[i]=sum(w)/p
    return out

def stoch(bars):
    raw=[None]*len(bars)
    for i in range(STOCH_K_PERIOD-1,len(bars)):
        w=bars[i-STOCH_K_PERIOD+1:i+1]; hh=max(x['h'] for x in w); ll=min(x['l'] for x in w)
        raw[i]=50.0 if hh==ll else 100*(bars[i]['c']-ll)/(hh-ll)
    k=sma(raw,STOCH_K_SMOOTH); d=sma(k,STOCH_D_PERIOD); return k,d

def resolve(side,entry,bars):
    tp=entry*(1+TP_PCT if side=='LONG' else 1-TP_PCT); sl=entry*(1-SL_PCT if side=='LONG' else 1+SL_PCT)
    best=0.0
    for b in bars:
        fav=(b['h']/entry-1)*100 if side=='LONG' else (1-b['l']/entry)*100
        best=max(best,fav)
        hit_tp=b['h']>=tp if side=='LONG' else b['l']<=tp
        hit_sl=b['l']<=sl if side=='LONG' else b['h']>=sl
        if hit_tp and hit_sl: return 'SAME',b['ts'],best
        if hit_tp: return 'WIN',b['ts'],best
        if hit_sl: return 'LOSS',b['ts'],best
    return 'TIME',bars[-1]['ts'] if bars else None,best

def main():
    data={}; idx={}; bodies=[]
    for inst in SYMBOLS:
        bars=fetch_1h(inst); k,d=stoch(bars)
        for i,b in enumerate(bars):
            b['k']=k[i]; b['d']=d[i]
            if START_TS<=b['ts']+D1H<=END_TS and b['o']>0: bodies.append(abs(b['c']/b['o']-1)*100)
        data[inst]=bars; idx[inst]={b['ts']+D1H:i for i,b in enumerate(bars)}
    body_med=statistics.median(bodies)
    def candidates(t):
        out=[]
        for inst in SYMBOLS:
            i=idx[inst].get(t)
            if i is None or i<22: continue
            bars=data[inst]; prev,cur=bars[i-1],bars[i]
            if cur['v']<=prev['v']: continue
            med=statistics.median([b['v'] for b in bars[i-3:i]])
            if med<=0 or cur['v']/med>SPIKE_CAP: continue
            if None in (prev['k'],prev['d'],cur['k'],cur['d']): continue
            if prev['k']<=prev['d'] and cur['k']>cur['d']: side='LONG'
            elif prev['k']>=prev['d'] and cur['k']<cur['d']: side='SHORT'
            else: continue
            if side=='LONG' and cur['c']<=cur['o']: continue
            if side=='SHORT' and cur['c']>=cur['o']: continue
            prior3=(prev['c']/bars[i-4]['c']-1)*100
            against=(side=='LONG' and prior3<0) or (side=='SHORT' and prior3>0)
            extreme=(side=='LONG' and cur['k']>=80) or (side=='SHORT' and cur['k']<=20)
            body=abs(cur['c']/cur['o']-1)*100
            out.append((RANK[inst],inst,side,cur['c'],i,against,prior3,extreme,body,cur['v']/prev['v'],cur['v']/med,cur['k'],cur['d']))
        out.sort(key=lambda x:x[0]); return out
    trades=[]; open_until=None; t=START_TS
    while t<=END_TS:
        if open_until is not None and open_until>t: t+=D1H; continue
        cs=candidates(t)
        if cs:
            rank,inst,side,entry,i,against,prior3,extreme,body,vprev,vmed,k,d=cs[0]
            mins=fetch_1m(inst,t); reason,exit_ts,best=resolve(side,entry,mins)
            trades.append({'t':t,'inst':inst,'side':side,'reason':reason,'exit':exit_ts,'mins':(exit_ts-t)/D1M if exit_ts else 999,'best':best,'against':against,'prior3':prior3,'extreme':extreme,'body':body,'large':body>body_med,'vprev':vprev,'vmed':vmed,'k':k,'d':d})
            open_until=exit_ts if exit_ts else t+2*D1H
        t+=D1H
    losses=[x for x in trades if x['reason']=='LOSS']; wins=[x for x in trades if x['reason']=='WIN']
    print(f'trades={len(trades)} wins={len(wins)} losses={len(losses)} time={sum(x["reason"]=="TIME" for x in trades)} same={sum(x["reason"]=="SAME" for x in trades)}')
    print(f'body_median={body_med:.3f}%')
    def n(pred,arr): return sum(1 for x in arr if pred(x))
    print('LOSS_PATTERNS')
    print(f'sl_within_10m={n(lambda x:x["mins"]<=10,losses)}/{len(losses)}')
    print(f'sl_within_30m={n(lambda x:x["mins"]<=30,losses)}/{len(losses)}')
    print(f'sl_within_60m={n(lambda x:x["mins"]<=60,losses)}/{len(losses)}')
    print(f'against_prior3h={n(lambda x:x["against"],losses)}/{len(losses)}')
    print(f'stoch_bad_extreme_long80_short20={n(lambda x:x["extreme"],losses)}/{len(losses)}')
    print(f'large_signal_body={n(lambda x:x["large"],losses)}/{len(losses)}')
    print(f'volume_gt1.5x_prev={n(lambda x:x["vprev"]>1.5,losses)}/{len(losses)}')
    print(f'got_+0.25pct_before_sl={n(lambda x:x["best"]>=0.25,losses)}/{len(losses)}')
    print(f'got_+0.40pct_before_sl={n(lambda x:x["best"]>=0.40,losses)}/{len(losses)}')
    print(f'avg_best_favorable_before_sl={statistics.mean(x["best"] for x in losses):.3f}%')
    print(f'long_losses={n(lambda x:x["side"]=="LONG",losses)} short_losses={n(lambda x:x["side"]=="SHORT",losses)}')
    print('WIN_COMPARISON')
    print(f'against_prior3h={n(lambda x:x["against"],wins)}/{len(wins)}')
    print(f'stoch_bad_extreme={n(lambda x:x["extreme"],wins)}/{len(wins)}')
    print(f'large_signal_body={n(lambda x:x["large"],wins)}/{len(wins)}')
    print(f'volume_gt1.5x_prev={n(lambda x:x["vprev"]>1.5,wins)}/{len(wins)}')
    print('LOSS_DETAILS')
    for x in losses:
        print(f'{datetime.fromtimestamp(x["t"]/1000,tz=timezone.utc).strftime("%Y-%m-%d %H:%M")} {x["inst"]} {x["side"]} sl_min={x["mins"]:.0f} best={x["best"]:.3f}% prior3={x["prior3"]:+.2f}% against={x["against"]} K/D={x["k"]:.1f}/{x["d"]:.1f} body={x["body"]:.2f}% vprev={x["vprev"]:.2f}x')
if __name__=='__main__': main()
