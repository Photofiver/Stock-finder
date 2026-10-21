"""Read-only candle comparison of current pivot rules with MA7 entry filters."""
import ast,copy,gzip,json,concurrent.futures
from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import research_ma7_last24_readonly as m
from blofin_pivot_tunnel import evaluate_tunnel,pivot_exit_transition
m.END=int(datetime.fromisoformat('2026-10-21T16:55:00+01:00').timestamp()*1000)
m.START=m.END-86400000;m.FIRST=m.START-240*m.STEP
namespace={'Decimal':Decimal}
tree=ast.parse(Path('blofin_volume_flip_both_live.py').read_text())
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='pivot_long_entry_filter')
exec(compile(ast.Module(body=[fn],type_ignores=[]),'current_entry_filter','exec'),namespace)
entry_filter=namespace['pivot_long_entry_filter']
def simulate(inst,bars,tick,mode):
 ts=sorted(bars);cl=[bars[t][3] for t in ts];out=[];pos=None;restart=0
 for i,t in enumerate(ts):
  end=t+m.STEP
  if end<=m.START:continue
  rows={k:bars[k] for k in ts[max(0,i-239):i+1]}
  o,h,l,c,v=bars[t]
  if pos:
   outcome=None;ambiguous=False
   if l<=pos['sl'] and h>=pos['tp']:outcome='SL';ambiguous=True;price=pos['sl']
   elif l<=pos['sl']:outcome='SL';price=pos['sl']
   elif h>=pos['tp']:outcome='TP';price=pos['tp']
   elif pivot_exit_transition(pos['pivot'],rows,end,'LONG'):outcome='TUNNEL_EXIT';price=c
   if outcome:
    out.append(dict(pos,exit_ms=end,outcome=outcome,ambiguous=ambiguous,gross_pct=(price/pos['entry']-1)*100))
    pos=None;restart=end
   else:continue
  tunnel=evaluate_tunnel(rows,end,restart)
  if not tunnel['entry_allowed'] or tunnel.get('tunnel_direction')!='LONG':continue
  prev=bars.get(t-m.STEP)
  if not prev or not(v>prev[4] and prev[3]<prev[0] and c>o):continue
  ma7=sum(cl[i-6:i+1])/7;oldma7=sum(cl[i-7:i])/7
  if mode=='above_ma7' and not c>ma7:continue
  if mode=='cross_ma7' and not(c>ma7 and cl[i-1]<=oldma7):continue
  sig={'side':'LONG','indicator_status':'OK','indicator_timeframe':'5m','indicator_signal_close_ms':end,'signal_close_ms':end,'sma10':sum(cl[i-9:i+1])/10,'volume_price_tunnel':tunnel}
  if not entry_filter(sig,{'expected_fill':str(c)})['allowed']:continue
  dt=Decimal(str(tick));tp=float((Decimal(str(tunnel['upper_price']))/dt).to_integral_value(rounding=ROUND_CEILING)*dt);sl=float((Decimal(str(tunnel['lower_price']))/dt).to_integral_value(rounding=ROUND_FLOOR)*dt)
  if not sl<c<tp:continue
  pos={'inst':inst,'entry_ms':end,'entry':c,'tp':tp,'sl':sl,'pivot':{'last_confirmed_high':copy.deepcopy(tunnel['last_confirmed_high']),'last_confirmed_low':copy.deepcopy(tunnel['last_confirmed_low'])}}
 if pos:out.append(dict(pos,outcome='OPEN',gross_pct=None,ambiguous=False))
 return out
def main():
 state=json.loads(Path('blofin_live_state.json').read_text());names=list(dict.fromkeys(state['last_top200']));assert len(names)==200
 instruments={x['instId']:x for x in m.get('/api/v1/market/instruments',{'instType':'SWAP'})}
 good={};errors={};results={x:[] for x in ['current','above_ma7','cross_ma7']}
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  fs={pool.submit(m.candles,n):n for n in names}
  for i,f in enumerate(concurrent.futures.as_completed(fs),1):
   n=fs[f]
   try:
    b=f.result();tick=float(instruments[n]['tickSize']);assert tick>0;good[n]=b
    for mode in results:results[mode]+=simulate(n,b,tick,mode)
   except Exception as e:errors[n]=str(e)
   if i%20==0:print('PROGRESS',i,'valid',len(good),'failed',len(errors),flush=True)
 counts={}
 for mode,rows in results.items():
  done=[r['gross_pct'] for r in rows if r['gross_pct'] is not None]
  counts[mode]={'LONG':{'entries':len(rows),'profit':sum(x>1e-10 for x in done),'loss':sum(x< -1e-10 for x in done),'flat':sum(abs(x)<=1e-10 for x in done),'open':sum(r['gross_pct'] is None for r in rows),'ambiguous_tp_sl':sum(r['ambiguous'] for r in rows)},'SHORT':{'entries':0,'profit':0,'loss':0}}
 summary={'start_uk':'2026-10-20 16:55','end_uk':'2026-10-21 16:55','complete_coins':len(good),'failed':errors,'counts':counts,'limitations':'Fixed current TOP200 from previous test; candle-close entries; current price/volume filters and tunnel TP/SL and pivot exit, one position per coin. No historical quote, execution delay, spread, capital/global position limit, fees, risk-stop. Both TP/SL hit within a 5m candle counted conservatively as SL.'}
 Path('ma7_tunnel24_results.json').write_text(json.dumps({'summary':summary,'trades':results},separators=(',',':')))
 with gzip.open('ma7_tunnel24_candles.json.gz','wt') as f:json.dump({'start':m.START,'end':m.END,'universe':names,'bars':good,'errors':errors},f,separators=(',',':'))
 print('MA7_TUNNEL_RESULT='+json.dumps(summary),flush=True)
if __name__=='__main__':main()
