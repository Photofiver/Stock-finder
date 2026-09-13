import os, sys, time, requests

BASE='https://openapi.blofin.com'
TIMEFRAME='1H'
REPORT_N=10
NTFY_TOPIC=os.getenv('NTFY_TOPIC','blofin-nhd0jt7wspfnhtitdlaowk1n').strip()

def get_json(path, params=None):
    r=requests.get(BASE+path, params=params, timeout=20); r.raise_for_status(); data=r.json()
    if str(data.get('code'))!='0': raise RuntimeError(f'BloFin API error: {data}')
    return data.get('data',[])

def ema(values, period):
    out=[None]*len(values)
    if len(values)<period: return out
    a=2/(period+1); prev=sum(values[:period])/period; out[period-1]=prev
    for i in range(period,len(values)):
        prev=a*values[i]+(1-a)*prev; out[i]=prev
    return out

def rsi(values, period=14):
    out=[None]*len(values)
    if len(values)<period+1: return out
    gains=[]; losses=[]
    for i in range(1,period+1):
        x=values[i]-values[i-1]; gains.append(max(x,0)); losses.append(max(-x,0))
    ag=sum(gains)/period; al=sum(losses)/period; out[period]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(period+1,len(values)):
        x=values[i]-values[i-1]; ag=(ag*(period-1)+max(x,0))/period; al=(al*(period-1)+max(-x,0))/period
        out[i]=100 if al==0 else 100-100/(1+ag/al)
    return out

def sma(values, period):
    out=[None]*len(values)
    for i in range(period-1,len(values)):
        w=values[i-period+1:i+1]
        if not any(x is None for x in w): out[i]=sum(w)/period
    return out

def stochastic(h,l,c,k_period=14,smooth_k=3,d_period=3):
    raw=[None]*len(c)
    for i in range(k_period-1,len(c)):
        hh=max(h[i-k_period+1:i+1]); ll=min(l[i-k_period+1:i+1]); raw[i]=50 if hh==ll else 100*(c[i]-ll)/(hh-ll)
    k=sma(raw,smooth_k); return k,sma(k,d_period)

def macd(values,fast=12,slow=26,signal=9):
    ef=ema(values,fast); es=ema(values,slow); line=[None]*len(values)
    for i in range(len(values)):
        if ef[i] is not None and es[i] is not None: line[i]=ef[i]-es[i]
    valid=[x for x in line if x is not None]; sig=ema(valid,signal); hist=[None]*len(values); j=0
    for i,x in enumerate(line):
        if x is not None:
            if sig[j] is not None: hist[i]=x-sig[j]
            j+=1
    return hist

def universe():
    live=set()
    for x in get_json('/api/v1/market/instruments'):
        if (x.get('state')=='live' and x.get('instType')=='SWAP' and x.get('contractType')=='linear' and x.get('settleCurrency')=='USDT'):
            live.add(x.get('instId',''))
    rows=[]
    for t in get_json('/api/v1/market/tickers'):
        inst=t.get('instId','')
        if inst not in live: continue
        try:
            last=float(t.get('last') or 0); op=float(t.get('open24h') or 0)
            if last>0 and op>0: rows.append(((last/op-1)*100,inst))
        except: pass
    rows.sort(key=lambda x:x[0],reverse=True)
    return [{'inst':inst,'change':change,'blofin_rank':n} for n,(change,inst) in enumerate(rows,1)]

def candles(inst):
    data=get_json('/api/v1/market/candles',{'instId':inst,'bar':TIMEFRAME,'limit':'120'}); rows=[]
    for c in data:
        try: rows.append((int(c[0]),float(c[1]),float(c[2]),float(c[3]),float(c[4]),float(c[5]),str(c[8]) if len(c)>8 else '0'))
        except: pass
    rows.sort(key=lambda x:x[0]); return [r for r in rows if r[6]=='1']

def analyze(inst):
    c=candles(inst)
    if len(c)<40: raise RuntimeError('za mało zamkniętych świec')
    o=[x[1] for x in c]; h=[x[2] for x in c]; l=[x[3] for x in c]; cl=[x[4] for x in c]; v=[x[5] for x in c]
    hist=macd(cl); rs=rsi(cl); k,d=stochastic(h,l,cl); i=len(c)-1; p=i-1
    if any(a[x] is None for a in (hist,rs,k,d) for x in (p,i)): raise RuntimeError('brak danych wskaźników')
    short=[hist[p]>0 and hist[i]<0,cl[i]<o[i],cl[p]>o[p],v[i]>v[p],rs[i]<rs[p],k[p]>=80 and k[i]<k[p],k[i]<d[i]]
    long=[hist[p]<0 and hist[i]>0,cl[i]>o[i],cl[p]<o[p],v[i]>v[p],rs[i]>rs[p],k[p]<=20 and k[i]>k[p],k[i]>d[i]]
    ss=sum(short); ls=sum(long); side='SHORT' if ss>=ls else 'LONG'; score=max(ss,ls)
    return {'inst':inst,'side':side,'score':score,'exact':score==7,'rsi':rs[i],'k':k[i],'d':d[i]}

def notify(text):
    r=requests.post(f'https://ntfy.sh/{NTFY_TOPIC}',data=text.encode(),headers={'Title':'BloFin 1H Scanner'},timeout=20); r.raise_for_status()

def main():
    try:
        coins=universe()
        if not coins: raise RuntimeError('Nie znaleziono aktywnych USDT-M')
        results=[]; errors=[]
        for coin in coins:
            try:
                x=analyze(coin['inst']); x.update(coin); results.append(x)
            except Exception as e: errors.append(f"{coin['inst']}: {e}")
            time.sleep(.12)
        results.sort(key=lambda x:(x['score'],-x['blofin_rank']),reverse=True)
        exact=[x for x in results if x['exact']]
        chosen=exact if exact else results[:REPORT_N]
        head=f"Przeskanowano {len(coins)} USDT-M. " + (f"PEŁNY SETUP ({len(exact)}):" if exact else f"Brak 7/7. Najlepsze {min(REPORT_N,len(results))}:")
        lines=[f"{n}. {x['inst']} {x['side']} — {x['score']}/7 | BloFin #{x['blofin_rank']} {x['change']:+.2f}% | RSI {x['rsi']:.1f} | STOCH {x['k']:.1f}/{x['d']:.1f}" for n,x in enumerate(chosen,1)]
        msg=head+'\n'+'\n'.join(lines)
        if errors: msg+=f'\nPominięto {len(errors)} (brak danych/błąd).'
        notify(msg); print(msg)
    except Exception as e:
        msg=f'BŁĄD SKANERA: {type(e).__name__}: {e}'; print(msg,file=sys.stderr)
        try: notify(msg)
        except: pass
        raise

if __name__=='__main__': main()
