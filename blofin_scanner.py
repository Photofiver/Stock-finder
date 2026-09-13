import os, sys, time, requests

BASE='https://openapi.blofin.com'
TOP_N=10
TIMEFRAME='1H'
NTFY_TOPIC=os.getenv('NTFY_TOPIC','blofin-nhd0jt7wspfnhtitdlaowk1n').strip()


def get_json(path, params=None):
    r=requests.get(BASE+path, params=params, timeout=20)
    r.raise_for_status()
    data=r.json()
    if str(data.get('code'))!='0':
        raise RuntimeError(f'BloFin API error: {data}')
    return data.get('data',[])


def ema(values, period):
    out=[None]*len(values)
    if len(values)<period: return out
    a=2/(period+1)
    prev=sum(values[:period])/period
    out[period-1]=prev
    for i in range(period,len(values)):
        prev=a*values[i]+(1-a)*prev
        out[i]=prev
    return out


def rsi(values, period=14):
    out=[None]*len(values)
    if len(values)<period+1: return out
    gains=[]; losses=[]
    for i in range(1,period+1):
        d=values[i]-values[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/period; al=sum(losses)/period
    out[period]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(period+1,len(values)):
        d=values[i]-values[i-1]
        g=max(d,0); l=max(-d,0)
        ag=(ag*(period-1)+g)/period
        al=(al*(period-1)+l)/period
        out[i]=100 if al==0 else 100-100/(1+ag/al)
    return out


def sma(values, period):
    out=[None]*len(values)
    for i in range(period-1,len(values)):
        w=values[i-period+1:i+1]
        if any(v is None for v in w): continue
        out[i]=sum(w)/period
    return out


def stochastic(highs,lows,closes,k_period=14,smooth_k=3,d_period=3):
    raw=[None]*len(closes)
    for i in range(k_period-1,len(closes)):
        hh=max(highs[i-k_period+1:i+1]); ll=min(lows[i-k_period+1:i+1])
        raw[i]=50.0 if hh==ll else 100*(closes[i]-ll)/(hh-ll)
    k=sma(raw,smooth_k); d=sma(k,d_period)
    return k,d


def macd(values,fast=12,slow=26,signal=9):
    ef=ema(values,fast); es=ema(values,slow)
    line=[None]*len(values)
    for i in range(len(values)):
        if ef[i] is not None and es[i] is not None:
            line[i]=ef[i]-es[i]
    valid=[x for x in line if x is not None]
    sigv=ema(valid,signal)
    hist=[None]*len(values)
    j=0
    for i,x in enumerate(line):
        if x is not None:
            s=sigv[j]
            if s is not None: hist[i]=x-s
            j+=1
    return hist


def top10():
    rows=[]
    for t in get_json('/api/v1/market/tickers'):
        inst=t.get('instId','')
        if not inst.endswith('-USDT'): continue
        try:
            last=float(t.get('last') or 0); op=float(t.get('open24h') or 0)
            if last<=0 or op<=0: continue
            rows.append(((last/op-1)*100,inst))
        except: pass
    rows.sort(reverse=True)
    return [x[1] for x in rows[:TOP_N]]


def candles(inst):
    data=get_json('/api/v1/market/candles',{'instId':inst,'bar':TIMEFRAME,'limit':'120'})
    rows=[]
    for c in data:
        try:
            rows.append((int(c[0]),float(c[1]),float(c[2]),float(c[3]),float(c[4]),float(c[5]),str(c[8]) if len(c)>8 else '1'))
        except: pass
    rows.sort(key=lambda x:x[0])
    closed=[r for r in rows if r[6]=='1']
    return closed if len(closed)>=40 else rows


def analyze(inst):
    c=candles(inst)
    if len(c)<40: return None
    o=[x[1] for x in c]; h=[x[2] for x in c]; l=[x[3] for x in c]; cl=[x[4] for x in c]; v=[x[5] for x in c]
    hist=macd(cl); rs=rsi(cl); k,d=stochastic(h,l,cl)
    i=len(c)-1; p=i-1
    if any(arr[idx] is None for arr in (hist,rs,k,d) for idx in (p,i)): return None
    short=(hist[p]>0 and hist[i]<0 and cl[i]<o[i] and cl[p]>o[p] and v[i]>v[p] and rs[i]<rs[p] and k[p]>=80 and k[i]<k[p] and k[i]<d[i])
    long=(hist[p]<0 and hist[i]>0 and cl[i]>o[i] and cl[p]<o[p] and v[i]>v[p] and rs[i]>rs[p] and k[p]<=20 and k[i]>k[p] and k[i]>d[i])
    if short: return f'{inst} SHORT | MACD✓ VOL✓ RSI↓ {rs[i]:.1f} STOCH↓ {k[i]:.1f}/{d[i]:.1f}'
    if long: return f'{inst} LONG | MACD✓ VOL✓ RSI↑ {rs[i]:.1f} STOCH↑ {k[i]:.1f}/{d[i]:.1f}'
    return None


def notify(text):
    r=requests.post(f'https://ntfy.sh/{NTFY_TOPIC}',data=text.encode('utf-8'),headers={'Title':'BloFin 1H Scanner'},timeout=20)
    r.raise_for_status()


def main():
    try:
        coins=top10()
        if len(coins)<TOP_N: raise RuntimeError(f'Pobrano tylko {len(coins)} coinów')
        hits=[]; errors=[]
        for inst in coins:
            try:
                x=analyze(inst)
                if x: hits.append(x)
            except Exception as e:
                errors.append(f'{inst}: {e}')
            time.sleep(.15)
        msg=('Sygnał 1H:\n'+'\n'.join(hits)) if hits else 'Brak pełnego setupu w Top 10 BloFin na zamkniętej świecy 1H.'
        if errors: msg+=f'\nBłędy dla {len(errors)} coinów.'
        notify(msg)
        print(msg)
    except Exception as e:
        msg=f'BŁĄD SKANERA: {type(e).__name__}: {e}'
        print(msg,file=sys.stderr)
        try: notify(msg)
        except: pass
        raise

if __name__=='__main__': main()
