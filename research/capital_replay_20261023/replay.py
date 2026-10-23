"""Offline current-code replay; no private API and no candle downloads.
Run from repository root: python research/capital_replay_20261023/replay.py DATA_DIR INSTRUMENTS_JSON
Checkpoints are per-symbol; delete checkpoints when code/metadata changes.
"""
import os,sys,json,gzip,ast,types,math,copy,heapq,itertools
from pathlib import Path
from decimal import Decimal,ROUND_FLOOR
from concurrent.futures import ProcessPoolExecutor
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ['LIVE_PIVOT_TUNNEL_ENABLED']='true';os.environ['BLOFIN_SHORT_ENTRY_MAX_SPREAD_PCT']='0'
# Load only pure functions from LIVE hourly; importing it normally initializes API configuration.
mod=types.ModuleType('blofin_live_hourly');mod.MACD_FAST=12;mod.MACD_SLOW=26;mod.MACD_SIGNAL=9
names=['rsi_wilder','ema_series','macd_series','stochastic_kd_series','d','clean_decimal','floor_step','size_for_notional']
tree=ast.parse((ROOT/'blofin_live_hourly.py').read_text());mod.Decimal=Decimal;mod.ROUND_FLOOR=ROUND_FLOOR
exec(compile(ast.Module(body=[x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name in names],type_ignores=[]),'pure_live','exec'),mod.__dict__)
sys.modules['blofin_live_hourly']=mod
import blofin_volume_flip_both_live as live
import blofin_pivot_tunnel as pivot
F=300000; START=1784999700000; END=1792775700000;D=86400000
OUT=Path(__file__).parent;CACHE=OUT/'checkpoints';CACHE.mkdir(exist_ok=True)
class Broker:
 d=staticmethod(lambda x:Decimal(str(x)))
 clean_decimal=staticmethod(str)
 save_state=staticmethod(lambda s:None)
 def private_request(self,method,path,params=None,body=None):
  if method=='POST':
   if 'newSlTriggerPrice' in body:self.detail['slTriggerPrice']=body['newSlTriggerPrice']
   if 'newTpTriggerPrice' in body:self.detail['tpTriggerPrice']=body['newTpTriggerPrice']
   return {'code':'0'}
  return self.detail.copy()

def one(args):
 file,meta=args;inst=Path(file).name.replace('.json.gz','');cp=CACHE/(inst+'.json')
 if cp.exists():return inst,len(json.load(open(cp))['trades'])
 data=json.load(gzip.open(file));raw=data['rows'];bars=[]
 for r in raw:
  if str(r[8])!='1':continue
  vals=list(map(float,r[1:6]));o,h,l,c,v=vals
  if not all(math.isfinite(x) for x in vals) or min(o,h,l,c)<=0 or v<0 or h<max(o,c) or l>min(o,c):continue
  bars.append(dict(ts=int(r[0]),o=o,h=h,l=l,c=c,v=v))
 bars.sort(key=lambda x:x['ts']); rows={b['ts']:(b['o'],b['h'],b['l'],b['c'],b['v']) for b in bars}
 trades=[];signals=0;missingmeta=not bool(meta); b=Broker()
 for i in range(48,len(bars)-1):
  x=bars[i];prev=bars[i-1];t=x['ts']+F
  if bars[i+1]['ts']!=t or prev['ts']!=x['ts']-F:continue
  if not(x['v']>prev['v'] and ((x['c']>x['o'] and prev['c']<prev['o']) or (x['c']<x['o'] and prev['c']>prev['o']))):continue
  small={z['ts']:rows[z['ts']] for z in bars[max(0,i-47):i+1]}
  tun=pivot.evaluate_tunnel(small,t)
  if not tun['entry_allowed']:continue
  side=tun['tunnel_direction'];sgn=1 if side=='LONG' else -1
  anch=tun['lower_anchors'] if sgn==1 else tun['upper_anchors']
  if anch[1]['confirmed_close_ms']!=t or (x['c']-x['o'])*sgn<=0:continue
  signals+=1
  if missingmeta:continue
  history=bars[max(0,i-199):i+1];ind=live._readonly_entry_indicators(history,t,side,x['c'])
  avg=sum(z['v'] for z in bars[i-20:i])/20
  features={'rsi':ind.get('rsi14'),'stoch_k':ind.get('stoch_k'),'stoch_kd':(ind.get('stoch_k') or 0)-(ind.get('stoch_d') or 0),'atr_pct':ind.get('atr14_pct'),'vol_strength':x['v']/avg if avg else 0,'vol_ratio':x['v']/prev['v'] if prev['v'] else 1000,'body_pct':abs(x['c']/x['o']-1)*100,'width_pct':(tun['upper_price']-tun['lower_price'])/x['c']*100,'ma10_gap':(x['c']/(ind.get('sma10') or x['c'])-1)*100,'ma20_gap':(x['c']/(ind.get('sma20') or x['c'])-1)*100,'macd_pct':(ind.get('macd_hist') or 0)/x['c']*100,'macd_delta_pct':((ind.get('macd_hist') or 0)-(ind.get('macd_hist_previous') or 0))/x['c']*100,'obv5_strength':(ind.get('obv_delta_5') or 0)/(avg or 1),'tp_sl_ratio':(tun['upper_price']-x['c'])/(x['c']-tun['lower_price']) if sgn==1 and x['c']>tun['lower_price'] else (x['c']-tun['lower_price'])/(tun['upper_price']-x['c']) if tun['upper_price']>x['c'] else 0}
  for slip in [0,.00025,.0005]:
   entry=bars[i+1]['o']*(1+sgn*slip);q={'expected_fill':str(entry),'adverse_gap_pct':str((entry/x['c']-1)*sgn*100),'spread_pct':'0'}
   if live.short_entry_extra_rejection(side,q):continue
   sig=dict(side=side,signal_close_ms=t,volume_price_tunnel=tun,**ind)
   if side=='LONG' and not live.pivot_long_entry_filter(sig,q)['allowed']:continue
   sized=mod.size_for_notional(Decimal(str(entry)),meta,Decimal('5'))
   if not sized:continue
   size,notional=sized
   pos=dict(inst=inst,side=side,reference_entry=str(entry),signal_close_ms=t,opened_ms=t,tp_sl_mode=live.EARLY_TUNNEL_TP_MODE,pivot_tunnel=copy.deepcopy(tun),tunnel_tp_line=str(tun['upper_price'] if sgn==1 else tun['lower_price']),tunnel_sl_line=str(tun['lower_price'] if sgn==1 else tun['upper_price']))
   try:tp,sl=live._protection_prices(b,pos,meta)
   except (ValueError,RuntimeError):continue
   pos.update(tp=str(tp),sl=str(sl),tpsl_id='offline')
   b.detail=dict(state='live',tpslId='offline',tpTriggerPrice=str(tp),slTriggerPrice=str(sl),tpTriggerPriceType='last',slTriggerPriceType='last')
   ambiguous=False;reason='END_MARK';exitp=bars[-1]['c'];et=END;closed=False
   for j in range(i+1,len(bars)):
    y=bars[j];tp=float(pos['tp']);sl=float(pos['sl']);close=y['ts']+F
    slhit=y['l']<=sl if sgn==1 else y['h']>=sl
    tphit=y['h']>=tp if sgn==1 else y['l']<=tp
    if slhit or tphit:
     ambiguous=slhit and tphit;reason='SL_BOTH' if ambiguous else 'SL' if slhit else 'TP'
     # SL-first if both hit; worse open across a stop, no favorable gap credit on TP.
     exitp=(min(y['o'],sl) if sgn==1 else max(y['o'],sl)) if slhit else tp
     et=close;closed=True;break
    sr={z['ts']:rows[z['ts']] for z in bars[max(0,j-47):j+1]}
    if pivot.second_boundary_bounce(pos,sr,close):
     if j+1<len(bars) and bars[j+1]['ts']==close:exitp=bars[j+1]['o'];et=close;reason='SECOND_TOUCH';closed=True
     break
    live._trail_confirmed_pivot_tpsl(b,{},pos,meta,sr,close,{'last':y['c']})
    # Invalid projected boundary is an explicit LIVE skip; existing protective prices remain.
   exitp*=1-sgn*slip
   quantity=float(size)*float(meta['contractValue']);gross=quantity*(exitp-entry)*sgn;fees=.0006*quantity*(entry+exitp)
   trades.append(dict(inst=inst,side=side,t=t,exit_t=et,earliest_anchor=min(p['open_ms'] for p in tun['upper_anchors']+tun['lower_anchors']),entry=entry,exit=exitp,notional=float(notional),quantity=quantity,gross=gross,fees=fees,net=gross-fees,ret=(gross-fees)/float(notional),slippage_per_side=slip,reason=reason,closed=closed,ambiguous=ambiguous,strength=features['vol_strength'],features=features))
 json.dump(dict(inst=inst,rows=len(bars),signals=signals,missingmeta=missingmeta,trades=trades),open(cp,'w'))
 return inst,len(trades)
if __name__=='__main__':
 meta={x['instId']:x for x in json.load(open(sys.argv[2]))['data']};files=sorted(Path(sys.argv[1]).rglob('*.json.gz'))
 with ProcessPoolExecutor(max_workers=8) as ex:
  for n,res in enumerate(ex.map(one,[(str(p),meta.get(p.name.replace('.json.gz',''))) for p in files]),1):print(n,res,flush=True)
