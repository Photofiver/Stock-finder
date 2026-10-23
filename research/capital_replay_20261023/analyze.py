import json,math,itertools,csv,statistics,hashlib
from pathlib import Path
from collections import Counter
import numpy as np
O=Path(__file__).parent;S=1784999700000;DAY=86400000;E=S+90*DAY
cuts={'train':(S,S+60*DAY),'validation':(S+60*DAY,S+75*DAY),'test':(S+75*DAY,E),'all90':(S,E)}
records=[json.load(open(p)) for p in sorted((O/'checkpoints').glob('*.json'))]
assert len(records)==200,len(records)
T=[t for r in records for t in r['trades']]
T.sort(key=lambda x:(x['t'],-x['strength'],x['inst']))

def match(t,rule):return all(t['features'].get(k) is not None and (t['features'][k]<=v if op=='<=' else t['features'][k]>=v) for k,op,v in rule)
def portfolio(ts,capital,start,end):
 active={};restart={};done=[];real=0.;stopped=False;maxpos=0;reject=Counter();maxcommit=0
 def settle(now):
  nonlocal real
  for k,x in list(active.items()):
   if x['exit_t']<=now:
    done.append(x);real+=x['net'];restart[k]=x['exit_t'];del active[k]
 for x in ts:
  if not start<=x['t']<end:continue
  settle(x['t'])
  if capital+real<=.9*capital:stopped=True
  if stopped:reject['risk_stop']+=1;continue
  if x['inst'] in active:reject['already_open']+=1;continue
  if x['earliest_anchor']<=restart.get(x['inst'],0):reject['restart_pivots']+=1;continue
  if len(active)>=4:reject['four_slots']+=1;continue
  committed=sum(z['notional'] for z in active.values())
  # Broker free USDT reserves entry commission; bankroll cap tracks realized NET like LIVE.
  available=capital+real-committed-sum(z['notional']*.0006 for z in active.values())
  spendable=min(min(capital,capital+real)-committed,available-.5)
  if spendable<5:reject['free_bankroll_below_5']+=1;continue
  active[x['inst']]=x;maxpos=max(maxpos,len(active));maxcommit=max(maxcommit,committed+x['notional'])
 settle(end)
 # Candidates crossing a split are excluded before this function: no future outcome used.
 closed=[x for x in done if x['closed']]
 def friction(x):
  s=x['slippage_per_side'];sgn=1 if x['side']=='LONG' else -1
  return x['quantity']*s*(x['entry']/(1+sgn*s)+x['exit']/(1-sgn*s))
 fee=sum(x['fees'] for x in closed);fr=sum(friction(x) for x in closed);gross=sum(x['gross'] for x in closed)+fr;net=gross-fee-fr
 return dict(capital=capital,n=len(closed),wins=sum(x['net']>0 for x in closed),losses=sum(x['net']<0 for x in closed),flat=sum(x['net']==0 for x in closed),gross_usdt=gross,fees_usdt=fee,friction_usdt=fr,net_usdt=net,gross_pct=100*gross/capital,fees_pct=100*fee/capital,friction_pct=100*fr/capital,net_pct=100*net/capital,max_positions=maxpos,max_committed=maxcommit,risk_stop=stopped,ambiguous=sum(x['ambiguous'] for x in closed),open_end=len(active)+sum(not x['closed'] for x in done),rejections=dict(reject),long_n=sum(x['side']=='LONG' for x in closed),short_n=sum(x['side']=='SHORT' for x in closed),long_net=sum(x['net'] for x in closed if x['side']=='LONG'),short_net=sum(x['net'] for x in closed if x['side']=='SHORT'))

def eligible(side,slip,period,rule=()):
 a,b=cuts[period]
 return [t for t in T if (side=='BOTH' or t['side']==side) and t['slippage_per_side']==slip and a<=t['t']<b and match(t,rule)]
# Training-only quartiles and beam search. Test outcomes never enter selection.
selected={};search={};rankings={}
for side in ['LONG','SHORT']:
 ts=[t for t in eligible(side,.00025,'train') if t['closed'] and t['exit_t']<=cuts['train'][1]];N=len(ts);features=sorted(ts[0]['features']) if ts else []
 atoms=[];masks=[];y=np.array([t['net'] for t in ts]);seen=set();evaluated=0
 for k in features:
  v=np.array([t['features'].get(k) if t['features'].get(k) is not None else np.nan for t in ts],float)
  for q in np.unique(np.nanquantile(v,[.25,.5,.75])):
   for op in ['<=','>=']:
    atoms.append((k,op,float(q)));masks.append((v<=q) if op=='<=' else (v>=q))
 beam=[((),np.ones(N,bool))];candidates=[]
 for depth in range(1,5):
  layer=[]
  for ids,mask in beam:
   for j,atom in enumerate(atoms):
    if ids and j<=ids[-1]:continue
    # Each feature once: interpretable intersections, no redundant bounds.
    if atom[0] in [atoms[z][0] for z in ids]:continue
    ix=ids+(j,);m=mask&masks[j];n=int(m.sum());evaluated+=1
    if n<30:continue
    key=m.tobytes()
    if key in seen:continue
    seen.add(key);net=float(y[m].sum());score=net # training absolute NET at fixed 5 USDT, not win rate
    layer.append((score,ix,m,n))
  layer.sort(key=lambda z:z[0],reverse=True);beam=[(ix,m) for _,ix,m,n in layer[:40]]
  candidates.extend([(sc,ix,n) for sc,ix,m,n in layer[:40]])
 # Training top 40 across depths, validation selects without opening test.
 candidates=sorted(candidates,reverse=True)[:40]; val=[]
 for trainnet,ids,n in candidates:
  rule=[atoms[j] for j in ids];vs=eligible(side,.00025,'validation',rule);a,b=cuts['validation'];p=portfolio(vs,100,a,b)
  val.append(dict(rule=rule,train_candidates=n,train_net_independent=trainnet,validation=p,validation_candidates=len(vs)))
 val.sort(key=lambda x:(x['validation']['net_usdt'] if x['validation']['n']>=5 else -1e9),reverse=True)
 selected[side]=val[0] if val else {'rule':[]};rankings[side]=val[:5]
 search[side]=dict(training_candidates=N,atoms=len(atoms),evaluated=evaluated,unique_masks=len(seen),depth_max=4,beam_width=40,validation_finalists=len(candidates),min_train_count=30,min_validation_fills=5)
# Save frozen rules before any holdout summaries.
json.dump(dict(selected=selected,search=search,rankings=rankings),open(O/'frozen_rules.json','w'),indent=2)
results=[]
for mode in ['baseline','filtered']:
 for side in ['LONG','SHORT','BOTH']:
  for slip in [0,.00025,.0005]:
   for period,(a,b) in cuts.items():
    ts=eligible(side,slip,period)
    if mode=='filtered':ts=[t for t in ts if match(t,selected[t['side']]['rule'])]
    for capital in [20,100,500,1000]:
     p=portfolio(ts,capital,a,b);p.update(mode=mode,side=side,slip_per_side_pct=100*slip,period=period,occurrences=len(ts));results.append(p)
# Top five validation-ranked patterns each get a holdout score; do not re-rank using test.
patterns=[]
for side,ranked in rankings.items():
 for rank,r in enumerate(ranked,1):
  for period,(a,b) in cuts.items():
   ts=eligible(side,.00025,period,r['rule'])
   for capital in [20,100,500,1000]:patterns.append(dict(side=side,validation_rank=rank,rule=r['rule'],period=period,occurrences=len(ts),**portfolio(ts,capital,a,b)))
# Training descriptive indicator separation, no test thresholds.
separation=[]
for side in ['LONG','SHORT']:
 ts=[t for t in eligible(side,.00025,'train') if t['closed'] and t['exit_t']<=cuts['train'][1]]
 for k in sorted(ts[0]['features']) if ts else []:
  w=[t['features'][k] for t in ts if t['net']>0 and t['features'][k] is not None];l=[t['features'][k] for t in ts if t['net']<0 and t['features'][k] is not None]
  if w and l:
   sd=float(np.std(w+l));separation.append(dict(side=side,feature=k,winner_median=statistics.median(w),loser_median=statistics.median(l),standardized_mean_difference=(statistics.mean(w)-statistics.mean(l))/(sd or 1)))
separation.sort(key=lambda x:abs(x['standardized_mean_difference']),reverse=True)
coverage=dict(coins=len(records),candles=sum(x['rows'] for x in records),raw_signals=sum(x['signals'] for x in records),missing_metadata=[x['inst'] for x in records if x['missingmeta']],scenario_candidate_trades=len(T),search=search)
json.dump(dict(coverage=coverage,results=results,patterns=patterns,separation=separation),open(O/'results.json','w'),indent=2)
for name,rows in [('capital_comparison',results),('patterns',patterns),('indicator_separation',separation),('candidate_trades',T)]:
 with open(O/(name+'.csv'),'w') as f:
  writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
print(json.dumps(coverage,indent=2));print(json.dumps(selected,indent=2));print(json.dumps([x for x in results if x['side']=='BOTH' and x['slip_per_side_pct']==.025 and x['period'] in ['all90','test']],indent=2))
