#!/usr/bin/env python3
"""Research-only 90d TP/SL/exit counterfactual replay on frozen Work entry candidates.
Does not import or modify the LIVE bot. Downloads no candles or private data.
5m OHLC: first stop when both touch; trailing updates AFTER candle close.
"""
import ast,bisect,csv,gzip,json,math,sys,collections
from pathlib import Path

HERE=Path(__file__).resolve().parent
ORIG=HERE.parent/'capital_replay_20261023'
START=1784999700000
DAY=86400000
END=START+90*DAY
SPLITS={'train':(START,START+60*DAY),'validation':(START+60*DAY,START+75*DAY),
        'test':(START+75*DAY,END),'all90':(START,END)}
CAPITALS=[20,100,500,1000]
SLIP=.00025
FEE=.0006
FIVE=300000

def variants():
    items=[]
    for tp in (.25,.4,.6,.8,1.2):
      for sl in (.2,.35,.5):
        for hold in (10,20,30,60):
          items.append(dict(id=f"tp{tp}_sl{sl}_h{hold}",tp=tp,sl=sl,hold=hold,be=None))
        for hold in (10,20,30):
          for trigger in (.20,.35,.50):
            items.append(dict(id=f"tp{tp}_sl{sl}_h{hold}_be{trigger}",
                   tp=tp,sl=sl,hold=hold,be=trigger))
    return items

def read_candidates():
    files=sorted((ORIG/'candidate_parts').glob('*.csv.gz'))
    assert len(files)==10, f"Need all ten existing candidate partitions: {len(files)}"
    out=[]
    for p in files:
      with gzip.open(p,'rt',newline='') as fh:
        for row in csv.DictReader(fh):
          if abs(float(row['slippage_per_side'])-SLIP)>1e-10:continue
          if row['side'] not in ('LONG','SHORT'):continue
          r={}
          for k in ('inst','side','reason'):r[k]=row[k]
          for k in ('t','exit_t','earliest_anchor'):r[k]=int(row[k])
          for k in ('entry','exit','notional','quantity','net','gross','fees','strength'):r[k]=float(row[k])
          r['closed']=row['closed']=='True';r['ambiguous']=row['ambiguous']=='True'
          r['key']=len(out)
          out.append(r)
    out.sort(key=lambda x:(x['t'],-x['strength'],x['inst']))
    for i,r in enumerate(out):r['key']=i
    assert len(out)>4000,f"unexpectedly small candidate pool {len(out)}"
    return out

def candle_paths(folder):
    paths=list(Path(folder).rglob('*.json.gz'))
    # Avoid unrelated csv.gz from branch by insisting correct payload.
    return {p.name.replace('.json.gz',''):p for p in paths if '-USDT.json.gz' in p.name}

def counterfactual(c,bars,ts,spec):
    sign=1 if c['side']=='LONG' else -1
    entry=c['entry'];qty=c['quantity']
    target=entry*(1+sign*spec['tp']/100)
    stop=entry*(1-sign*spec['sl']/100)
    trigger=spec['be']
    best=entry
    start=bisect.bisect_left(ts,c['t'])
    if start==len(bars) or ts[start]!=c['t']:
        return None
    maxj=min(len(bars),start+int(spec['hold']/5)+1)
    for j in range(start,maxj):
      time0,o,h,l,cl=bars[j]
      if j>start and time0!=bars[j-1][0]+FIVE:return None
      hit_stop=(l<=stop if sign==1 else h>=stop)
      hit_target=(h>=target if sign==1 else l<=target)
      if hit_stop or hit_target:
        price=(min(o,stop) if sign==1 else max(o,stop)) if hit_stop else target
        reason=('BOTH_SL_FIRST' if hit_stop and hit_target else 'SL' if hit_stop else 'TP')
        return outcome(c,price,time0+FIVE,reason,hit_stop and hit_target)
      # Stop update only AFTER this candle closes: no lookahead execution.
      peak=h if sign==1 else l
      best=(max(best,peak) if sign==1 else min(best,peak))
      # A stop cannot be amended beyond the current post-close price.
      # The 0.20% protective level exceeds the modeled ~0.17% round-trip cost.
      if trigger is not None and sign*(best/entry-1)*100 >= trigger:
        be=entry*(1+sign*.20/100)
        if sign*(cl-be)>0:
          stop=max(stop,be) if sign==1 else min(stop,be)
      if (j-start+1)*5>=spec['hold']:
        if j+1>=len(bars) or bars[j+1][0]!=time0+FIVE:return None
        return outcome(c,bars[j+1][1],time0+FIVE,'TIME',False)
    return None

def outcome(c,price,closed_ms,reason,ambiguous):
    sign=1 if c['side']=='LONG' else -1
    # Observed price before modeled adverse close fill.
    exit_fill=price*(1-sign*SLIP)
    gross=c['quantity']*sign*(exit_fill-c['entry'])
    fees=FEE*c['quantity']*(c['entry']+exit_fill)
    return (closed_ms,gross-fees,reason,ambiguous)

def portfolio(candidates, outcomes, period, capital):
    start,end=SPLITS[period]
    act={};restart={};closed=[];real=0.;stopped=False;reject=collections.Counter()
    max_positions=0
    def settle(now):
      nonlocal real
      for inst,(c,v) in list(act.items()):
        if v[0]<=now:
          real+=v[1];restart[inst]=v[0];closed.append((c,v));del act[inst]
    for c in candidates:
      if not start<=c['t']<end:continue
      settle(c['t'])
      if capital+real<=.9*capital:stopped=True
      if stopped:reject['risk_stop']+=1;continue
      v=outcomes.get(c['key'])
      if v is None or v[0]>end:
        reject['missing_or_crossing_exit']+=1;continue
      inst=c['inst']
      if inst in act:reject['already_open']+=1;continue
      if c['earliest_anchor']<=restart.get(inst,0):reject['restart_pivots']+=1;continue
      if len(act)>=4:reject['four_slots']+=1;continue
      committed=sum(x['notional'] for x,_ in act.values())
      available=capital+real-committed-sum(x['notional']*FEE for x,_ in act.values())
      spendable=min(min(capital,capital+real)-committed,available-.5)
      if spendable<5:reject['free_bankroll_below_5']+=1;continue
      act[inst]=(c,v);max_positions=max(max_positions,len(act))
    settle(end)
    # Identical settlement accounting to the original Work capital model.
    return dict(n=len(closed),wins=sum(v[1]>0 for c,v in closed),
       losses=sum(v[1]<0 for c,v in closed),
       net_usdt=round(sum(v[1] for c,v in closed),7),
       net_pct=round(100*sum(v[1] for c,v in closed)/capital,5),
       risk_stop=stopped,ambiguous=sum(v[3] for c,v in closed),
       max_positions=max_positions,rejections=dict(reject),
       tp=sum(v[2]=='TP' for c,v in closed),sl=sum(v[2]=='SL' for c,v in closed),
       time=sum(v[2]=='TIME' for c,v in closed),
       breakeven_stop=sum(v[2]=='SL' and v[1]>0 for c,v in closed))

def baseline_outcomes(candidates):
    return {c['key']:(c['exit_t'],c['net'],c['reason'],c['ambiguous'])
            for c in candidates if c['closed']}

def main(folder):
    candidates=read_candidates()
    paths=candle_paths(folder)
    instruments={c['inst'] for c in candidates}
    missing=instruments-set(paths)
    if missing:raise RuntimeError(f"Missing OHLCV files for {len(missing)} candidate symbols: {sorted(missing)[:10]}")
    base=baseline_outcomes(candidates)
    # Cross-check exact original baseline before running research.
    expected={'20':-2.050609,'100':-10.004899,'500':-50.037080,'1000':-50.037080}
    checks={}
    for capital in CAPITALS:
      p=portfolio(candidates,base,'all90',capital)
      delta=p['net_usdt']-expected[str(capital)]
      checks[str(capital)]={'observed_net':p['net_usdt'],'expected_net':expected[str(capital)],'difference':round(delta,8),'n':p['n']}
    (HERE/'baseline_check.json').write_text(json.dumps(checks,indent=2)+'\n')
    if max(abs(x['difference']) for x in checks.values())>.01:
      raise RuntimeError('Baseline portfolio mismatch: inspect original model before claiming comparison: '+json.dumps(checks))
    specifications=variants()
    byinst=collections.defaultdict(list)
    for c in candidates:byinst[c['inst']].append(c)
    outputs={s['id']:{} for s in specifications}
    missing_entry_candles=0
    for n,inst in enumerate(sorted(instruments),1):
      with gzip.open(paths[inst],'rt') as fh:raw=json.load(fh)['rows']
      bars=sorted(((int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]))
                   for x in raw if x[8]=='1'),key=lambda r:r[0])
      ts=[x[0] for x in bars]
      for c in byinst[inst]:
        for spec in specifications:
          v=counterfactual(c,bars,ts,spec)
          if v is None:missing_entry_candles+=1
          else:outputs[spec['id']][c['key']]=v
      if n%25==0:print('Processed',n,'/',len(instruments),'symbols',flush=True)
    # Select strictly by training and validation; never optimize on holdout.
    rows=[]
    for spec in specifications:
      outs=outputs[spec['id']]
      train=portfolio(candidates,outs,'train',100)
      validation=portfolio(candidates,outs,'validation',100)
      rows.append(dict(id=spec['id'],parameters=spec,train=train,validation=validation,
                       valid_count=len(outs)))
    # Frozen qualifying thresholds: >= 100 training and >= 30 validation executions
    eligible=[r for r in rows if r['train']['n']>=100 and r['validation']['n']>=30]
    eligible.sort(key=lambda r:(r['validation']['net_usdt'],r['train']['net_usdt']),reverse=True)
    ranking=eligible[:15]
    target=[x['id'] for x in ranking]
    baseline_table={}
    for period in ('train','validation','test','all90'):
      baseline_table[period]={str(cap):portfolio(candidates,base,period,cap) for cap in CAPITALS}
    # Holdout only now, after selection frozen.
    finals=[]
    for r in ranking:
      results={period:{str(c):portfolio(candidates,outputs[r['id']],period,c)
              for c in CAPITALS} for period in ('train','validation','test','all90')}
      finals.append(dict(id=r['id'],parameters=r['parameters'],results=results,
              valid_count=r['valid_count']))
    # Transparent descriptive last-period stats for ALL variants; they are not selection metrics.
    summary=dict(status='RESEARCH_ONLY_LIVE_UNCHANGED',
      data_run_id=39994070142,source_replay_branch='research/live-capital-replay-20261023',
      candidates_at_0_025pct_slip=len(candidates),candles_symbols=len(instruments),
      variants_tested=len(specifications),
      methods=['SL-first ambiguous candle','No same-bar stop updates',
        'Entries from frozen 90d replay candidate files','5m OHLC fills with 0.025% per-side adverse execution and 0.06% per-side fee',
        '20/100/500/1000 total bankroll, up to 4 entries of max 5 USDT, 10% circuit breaker',
        '60/15/15 chronology; best variants selected on validation, test untouched during selection'],
      baseline_reproduction=checks,baseline=baseline_table,
      variants_train_validation=rows,
      holdout_of_validation_selected=finals,
      selection_rule='At least 100 training trades and 30 validation trades, best validation USDT return at capital 100; no holdout-based selection',
      caveats=['Counterfactual exits are different strategies, not faithful original tunnel trailing code',
         'Current TOP200, not historical universe, missing order book and exchange spread, assumed slippage/funding omitted',
         '90-day OHLC candles cannot determine inside-bar sequence, SL-first is conservative',
         'A 10% drawdown stop can make 90-day results comparable only as constrained portfolio paths',
         'NO pattern selection is guaranteed to generalize to live fills or future periods'])
    (HERE/'exit_grid_report.json').write_text(json.dumps(summary,indent=2)+'\n')
    lines=['# BloFin 90-day exit policy research (READ-ONLY)',
       f"Candidates: {len(candidates)} (middle scenario) | variants: {len(specifications)}",
       'Baseline reproduction (all 90 days): '+json.dumps(checks),
       '',
       '## Baseline portfolio (middle execution cost 0.025% each way)']
    for period in ('train','validation','test','all90'):
      x=baseline_table[period]['100']
      lines.append(f"- {period}: {x['n']} trades, {x['wins']} W / {x['losses']} L, net {x['net_usdt']:+.5f} USDT on 100 USDT")
    lines.extend(['','## Top variations chosen on training/validation, NEVER on independent test'])
    for i,r in enumerate(finals,1):
      va=r['results']['validation']['100'];te=r['results']['test']['100']
      lines.append(f"{i}. {r['id']}: validation {va['n']} trades, {va['wins']}W/{va['losses']}L, {va['net_usdt']:+.5f} USDT; TEST {te['n']} trades, {te['wins']}W/{te['losses']}L, {te['net_usdt']:+.5f} USDT")
    lines.extend(['','## Limitations','5m OHLC cannot reproduce broker fills or unknown intra-bar order; cost model is estimated, not verified exact fills.', 'Not a recommendation for LIVE until independent robustness review.'])
    (HERE/'exit_grid_report.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines[:45]),flush=True)

if __name__=='__main__':
    main(sys.argv[1] if len(sys.argv)>1 else '.')
