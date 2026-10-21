"""Public BloFin candles only. No orders, credentials or LIVE state writes."""
import concurrent.futures,json,time,threading,statistics
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import requests
STEP=300000
END=int(time.time()*1000)//STEP*STEP
START=END-86400000
FIRST=START-8*STEP
lock=threading.Lock()
last=0.
def get(path,params):
 global last
 for attempt in range(4):
  try:
   with lock:
    time.sleep(max(0,.15-(time.monotonic()-last)));last=time.monotonic()
   r=requests.get('https://openapi.blofin.com'+path,params=params,timeout=25);r.raise_for_status();d=r.json()
   if str(d.get('code'))!='0':raise ValueError(str(d)[:200])
   return d['data']
  except Exception:
   if attempt==3:raise
   time.sleep(1+attempt)
def candles(inst):
 bars={};cursor=END
 for page in range(6):
  rows=get('/api/v1/market/candles',{'instId':inst,'bar':'5m','after':str(cursor),'limit':'100'})
  if not rows:break
  for r in rows:
   t=int(r[0])
   if len(r)>=9 and str(r[8])=='1' and FIRST<=t<END:
    bars[t]=[float(r[i]) for i in range(1,6)]
  oldest=min(int(r[0]) for r in rows)
  if oldest<=FIRST:break
  if oldest>=cursor:raise ValueError('Pagination did not advance')
  cursor=oldest
 missing=[t for t in range(FIRST,END,STEP) if t not in bars]
 if missing:raise ValueError('Missing '+str(len(missing))+' candles; first '+str(missing[0]))
 return bars
def evaluate(inst,bars):
 ts=sorted(bars);cl=[bars[t][3] for t in ts];ma=[None]*6+[sum(cl[i-6:i+1])/7 for i in range(6,len(cl))]
 trades=[]
 for i in range(7,len(ts)):
  closed=ts[i]+STEP
  if not START<closed<=END:continue
  side=1 if cl[i]>ma[i] and cl[i-1]<=ma[i-1] else -1 if cl[i]<ma[i] and cl[i-1]>=ma[i-1] else 0
  if not side:continue
  exit_i=next((j for j in range(i+1,len(ts)) if (cl[j]-ma[j])*side<0),None)
  pnl=None if exit_i is None else (cl[exit_i]/cl[i]-1)*100*side
  trades.append({'inst':inst,'side':'LONG' if side==1 else 'SHORT','entry_ms':closed,'entry':cl[i],'exit_ms':None if exit_i is None else ts[exit_i]+STEP,'gross_pct':pnl})
 return trades
def main():
 state=json.loads(Path('blofin_live_state.json').read_text())
 names=list(dict.fromkeys(state.get('last_top200') or []))
 if len(names)!=200:raise RuntimeError('Expected current 200-coin universe, got '+str(len(names)))
 good={};errors={};trades=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  fs={pool.submit(candles,n):n for n in names}
  for k,f in enumerate(concurrent.futures.as_completed(fs),1):
   n=fs[f]
   try:good[n]=f.result();trades+=evaluate(n,good[n])
   except Exception as e:errors[n]=str(e)
   if k%20==0:print('PROGRESS',k,'valid',len(good),'failed',len(errors),flush=True)
 counts={}
 for side in ['LONG','SHORT']:
  t=[x for x in trades if x['side']==side];closed=[x['gross_pct'] for x in t if x['gross_pct'] is not None]
  counts[side]={'entries':len(t),'profit':sum(p>1e-10 for p in closed),'loss':sum(p< -1e-10 for p in closed),'flat':sum(abs(p)<=1e-10 for p in closed),'open':sum(x['gross_pct'] is None for x in t),'mean_gross_pct':statistics.mean(closed) if closed else None}
 uk=lambda t:datetime.fromtimestamp(t/1000,ZoneInfo('Europe/London')).isoformat()
 summary={'start_uk':uk(START),'end_uk':uk(END),'universe':'current bot last_top200, fixed throughout test','requested_coins':len(names),'complete_coins':len(good),'failed':errors,'counts':counts,'rules':'5m SMA7 of close; enter on closed crossover; exit on first close strictly opposite SMA7; no TP/SL, fees or slippage; signals independent across coins; open positions excluded from wins/losses'}
 Path('ma7_last24_results.json').write_text(json.dumps({'summary':summary,'trades':trades},separators=(',',':')))
 print('MA7_RESULT='+json.dumps(summary),flush=True)
if __name__=='__main__':main()
