#!/usr/bin/env python3
"""Research-only 90-day lookback pattern/width/body/ATR filter test.
Reads EXACT previously downloaded OHLCV and frozen LIVE replay candidates.
Never contacts an exchange, orders, or changes LIVE.
Chronological discovery 60d -> selection/validation 15d -> holdout 15d.
"""
import ast,bisect,collections,csv,gzip,json,math,statistics,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'research/capital_replay_20261023'
HERE=Path(__file__).resolve().parent
DAY=86400000; S=1784999700000; E=S+90*DAY; F=300000
SPLIT={'train':(S,S+60*DAY),'validation':(S+60*DAY,S+75*DAY),
       'test':(S+75*DAY,E),'all90':(S,E)}
SLIP=.00025; FEE=.0006
CAPS=(20,100,500,1000)

def load():
    files=sorted((BASE/'candidate_parts').glob('*.csv.gz'))
    assert len(files)==10, "Expected 10 frozen candidate gzip parts"
    trades=[]
    for p in files:
      with gzip.open(p,'rt',newline='') as f:
        for r in csv.DictReader(f):
          if abs(float(r['slippage_per_side'])-SLIP)>1e-9:continue
          d={k:r[k] for k in ('inst','side','reason')}
          for k in ('t','exit_t','earliest_anchor'):d[k]=int(r[k])
          for k in ('net','gross','fees','notional','entry','quantity','strength'):d[k]=float(r[k])
          d['closed']=r['closed']=='True'
          d['features']=ast.literal_eval(r['features'])
          trades.append(d)
    trades.sort(key=lambda x:(x['t'],-x['strength'],x['inst']))
    assert len(trades)==5994,("candidate count mismatch",len(trades))
    for i,t in enumerate(trades):t['i']=i
    return trades

def candles(path):
    with gzip.open(path,'rt') as f:data=json.load(f)
    bars=sorted([(int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])) for x in data['rows']
                 if str(x[8])=='1'],key=lambda z:z[0])
    return bars,[x[0] for x in bars]

def seq_features(t,bars,timestamps):
    ix=bisect.bisect_left(timestamps,t['t'])
    if ix==len(timestamps) or timestamps[ix]!=t['t'] or ix<7:return None
    b=bars[ix-7:ix]
    if any(b[j][0]!=b[j-1][0]+F for j in range(1,len(b))):return None
    side=1 if t['side']=='LONG' else -1
    codes=['U' if (z[4]-z[1])*side>0 else 'D' if (z[4]-z[1])*side<0 else 'X' for z in b]
    # Last bar is the confirmed signal bar; use NO candles after entry.
    patterns={f"exact_{n}_{''.join(codes[-n:])}" for n in range(2,7)}
    z=b[-1];pr=b[-2]
    pre=b[-6:-1]
    same=sum(v=='U' for v in codes[-6:])
    patterns|={f"same_{n}_{sum(v=='U' for v in codes[-n:])}" for n in range(2,7)}
    if side*(z[4]/b[-4][4]-1)>0:patterns.add('momentum3')
    if side*(z[4]/b[-7][4]-1)>0:patterns.add('momentum6')
    if side==1 and z[4]>max(v[2] for v in b[-4:-1]) or side==-1 and z[4]<min(v[3] for v in b[-4:-1]):patterns.add('breakout3')
    if side==1 and all(b[j][3]>=b[j-1][3] for j in (-3,-2,-1)) or side==-1 and all(b[j][2]<=b[j-1][2] for j in (-3,-2,-1)):patterns.add('swing3')
    if all(b[j][5]>b[j-1][5] for j in (-3,-2,-1)):patterns.add('volume_up3')
    if z[5]>2*statistics.median(v[5] for v in b[-6:-1]):patterns.add('volume_spike2')
    if abs(z[4]-z[1])>abs(pr[4]-pr[1]):patterns.add('body_vs_previous')
    if (z[2]-z[3]) > statistics.median(v[2]-v[3] for v in b[-6:-1]):patterns.add('range_expansion')
    if side==1 and z[1]<=pr[4] and z[4]>=pr[1] or side==-1 and z[1]>=pr[4] and z[4]<=pr[1]:patterns.add('engulfing')
    if side*(z[4]/pr[4]-1)>.002:patterns.add('last_candle_020pct')
    return patterns

def portfolio(items,capital,period):
    a,b=SPLIT[period]
    active={};restart={};done=[];real=0.;stopped=False
    rejections=collections.Counter()
    def settle(now):
        nonlocal real
        for k,v in list(active.items()):
            if v['exit_t']<=now:
                done.append(v);real+=v['net'];restart[k]=v['exit_t'];del active[k]
    for t in items:
      if not a<=t['t']<b:continue
      settle(t['t'])
      if capital+real<=.9*capital:stopped=True
      if stopped:rejections['risk_stop']+=1;continue
      if t['inst'] in active:rejections['already_open']+=1;continue
      if t['earliest_anchor']<=restart.get(t['inst'],0):rejections['restart_pivots']+=1;continue
      if len(active)>=4:rejections['four_slots']+=1;continue
      committed=sum(z['notional'] for z in active.values())
      available=capital+real-committed-sum(z['notional']*.0006 for z in active.values())
      spendable=min(min(capital,capital+real)-committed,available-.5)
      if spendable<5:rejections['free_bankroll_below_5']+=1;continue
      active[t['inst']]=t
    settle(b)
    closed=[x for x in done if x['closed']]
    net=sum(x['net'] for x in closed)
    return dict(n=len(closed),wins=sum(x['net']>0 for x in closed),
      losses=sum(x['net']<0 for x in closed),net_usdt=round(net,7),
      net_pct=round(net/capital*100,5),risk_stop=stopped,
      rejected=dict(rejections))

def qtile(a,p):
    x=sorted(a);idx=(len(x)-1)*p;low=int(idx);high=math.ceil(idx)
    return x[low]+(x[high]-x[low])*(idx-low)

def mask(trades,rule,side):
    cuts=rule['thresholds'];token=rule['pattern']
    return [t for t in trades if t['side']==side and t.get('seq') is not None and
      all(t['features'][k] is not None and t['features'][k]>=v for k,v in cuts.items()) and
      (token is None or token in t['seq'])]

def main(archive_dir):
    T=load()
    paths={p.name[:-8]:p for p in Path(archive_dir).rglob('*.json.gz') if p.name.endswith('-USDT.json.gz')}
    assert len(paths)==200,("Expected 200 pre-downloaded symbols",len(paths))
    groups=collections.defaultdict(list)
    for t in T:groups[t['inst']].append(t)
    missing=set(groups)-set(paths);assert not missing,missing
    missed=0
    for i,inst in enumerate(sorted(groups),1):
      bars,ts=candles(paths[inst])
      for t in groups[inst]:
        t['seq']=seq_features(t,bars,ts)
        if t['seq'] is None:missed+=1
      if i%50==0:print(f"Feature extraction {i}/200",flush=True)
    assert missed<100,("Too many missing history features",missed)
    base={str(c):{period:portfolio(T,c,period) for period in ('train','validation','test','all90')}
          for c in CAPS}
    expected={20:-2.050609,100:-10.004899,500:-50.03708,1000:-50.03708}
    verified={str(c):dict(expected=expected[c],got=base[str(c)]['all90']['net_usdt'])
              for c in CAPS}
    assert all(abs(verified[str(c)]['got']-expected[c])<.00002 for c in CAPS),verified
    # Thresholds are derived SOLELY from first 60d, and quantile levels are fixed.
    threshold={}
    for side in ('LONG','SHORT'):
      a=[t for t in T if t['side']==side and S<=t['t']<S+60*DAY and
          t['closed'] and t['exit_t']<=S+60*DAY and t.get('seq') is not None]
      threshold[side]={k:{str(int(p*100)):qtile([t['features'][k] for t in a],p)
                for p in (.25,.50,.75)} for k in ('width_pct','body_pct','atr_pct')}
    all_records=[]
    nominated=[]
    for side in ('LONG','SHORT'):
      side_train=[t for t in T if t['side']==side and S<=t['t']<S+60*DAY and
          t['exit_t']<=S+60*DAY and t['closed'] and t.get('seq') is not None]
      side_val=[t for t in T if t['side']==side and
          S+60*DAY<=t['t']<S+75*DAY and t.get('seq') is not None]
      # Count recurring exact 2-6-candle sequences and broader structural patterns.
      tokens=collections.Counter(z for t in side_train for z in t['seq'])
      token_choices=[None]+[key for key,n in sorted(tokens.items())
                    if n>=30 and sum(key in t['seq'] for t in side_val)>=8]
      print(side,'eligible structural / exact pattern tokens:',len(token_choices),flush=True)
      candidates=[]
      for wa in (25,50,75):
       for bo in (25,50,75):
        for at in (25,50,75):
          thresh={k:threshold[side][k][str(q)] for k,q in
                   (('width_pct',wa),('body_pct',bo),('atr_pct',at))}
          # Even three-feature-only rules compete against sequence-extended rules.
          for token in token_choices:
            rule={'side':side,'thresholds':thresh,'quartiles':{'width':wa,'body':bo,'atr':at},
                  'pattern':token}
            tr=mask(side_train,rule,side)
            va=mask(side_val,rule,side)
            if len(tr)<25 or len(va)<8:continue
            raw=sum(t['net'] for t in tr)
            wins=sum(t['net']>0 for t in tr)
            # Train-only ranking: favor sufficiently common, fee-positive patterns.
            candidates.append((raw, len(tr),wins,rule,tr,va))
      candidates.sort(key=lambda x:(x[0],x[1]),reverse=True)
      # Discover on training, then inspect and select on validation; holdout untouched.
      toptrain=candidates[:250]
      uniq=set()
      validations=[]
      for train_net,n,w,rule,tr,va in toptrain:
          # Identical accepted candidate masks should not inflate number of discoveries.
          key=(tuple(t['i'] for t in tr),tuple(t['i'] for t in va))
          if key in uniq:continue
          uniq.add(key)
          train_pf=portfolio(tr,100,'train')
          valid_pf=portfolio(va,100,'validation')
          row={'rule':rule,'raw_train':round(train_net,7),
               'n_train_candidates':n,'train_wins':w,'train':train_pf,
               'validation':valid_pf,'n_validation_candidates':len(va)}
          validations.append(row)
      validations.sort(key=lambda r:(r['validation']['net_usdt'],r['train']['net_usdt']),reverse=True)
      # Never choose rules based on holdout: fixed minimum evidence and >0 validation net.
      passes=[r for r in validations if r['train']['n']>=25 and
              r['validation']['n']>=10 and r['train']['net_usdt']>0 and
              r['validation']['net_usdt']>0]
      chosen=passes[0] if passes else None
      nominated.append((side,chosen,validations))
      print(side,'searched',len(candidates),'train-ranked rules;',len(validations),
            'unique finalists;',len(passes),'passing validation',flush=True)
      all_records.append({'side':side,'search_count':len(candidates),
        'unique_train_finalists':len(validations),'passed_validation':len(passes),
        'selected_rule':chosen['rule'] if chosen else None,
        'top_validation_first_10':validations[:10]})
    # Opening holdout only AFTER freezing selection from validation.
    finalists=[]
    for side,chosen,vals in nominated:
      for rank,row in enumerate(vals[:10],1):
        rule=row['rule']
        matched=mask(T,rule,side)
        result={str(c):{p:portfolio(matched,c,p) for p in ('train','validation','test','all90')}
                for c in CAPS}
        finalists.append({'side':side,'rank':rank,'passed_validation':bool(chosen and rule==chosen['rule']),
            'rule':rule,'n_candidates':len(matched),'outcomes':result})
    # Combine chosen LONG and SHORT for a single capital constrained portfolio.
    selected=[mask(T,r['rule'],side) for side,r,_ in nominated if r]
    joined=sorted((t for ss in selected for t in ss),key=lambda t:(t['t'],-t['strength'],t['inst']))
    combined={str(c):{p:portfolio(joined,c,p) for p in ('train','validation','test','all90')}
                for c in CAPS} if selected else None
    report=dict(status='COMPLETE_READ_ONLY_NO_LIVE_CHANGES',
      data_run=39994070142,candidate_source='research/live-capital-replay-20261023',
      frozen_candidates=len(T),sequences_missing=missed,
      method='Exact 2-6 bar trailing red/green sequences relative to trade direction + 2-6 bar U counts + structural price/volume patterns, intersected with training-quantile thresholds for width/body/ATR',
      train_quantiles=threshold,baseline_reproduced=verified,
      baseline=base,search=all_records,holdout_first10_per_direction=finalists,
      combined_validated_only=combined,
      cautions=[
       'No historical bid/ask or funding; assumes 0.06% per side commissions and 0.025% per side modeled slippage.',
       'No future bars in signals. Candle patterns use signal candle and up to five older candles.',
       'ALL90 includes in-sample and must not be used to select strategies.',
       'Small validation samples or failure to validate mean do not infer profit.',
       'Only frozen current-TOP200 universe; not historical dynamic ranking.',
       'Trades use original LIVE replay counterfactual fills; possible model artifacts remain.',
       'Filter search bounded to the declared thresholds and candle pattern family, not exhaustive across all indicators.'])
    HERE.mkdir(exist_ok=True)
    (HERE/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    lines=['# 90d candle sequence + width/body/ATR entry-filter study',
           'RESEARCH ONLY: LIVE unchanged; no new market candle downloads.',
           f"Reused frozen 5m candidate entries: {len(T)}; candidates missing trailing sequence: {missed}.",
           'Data: 5,142,770 5m candles for 200 BloFin coins, GitHub Actions run 39994070142.',
           'Backtest cost: 0.06% fee + 0.025% modeled execution, per side.',
           'Train 60d -> validation 15d -> holdout test 15d, chronologically.', '',
           '## Baseline (100 USDT, 5 USDT each position)',
          ]
    for p in ('train','validation','test','all90'):
      x=base['100'][p];lines.append(f"- {p}: {x['n']} trades, {x['wins']}W/{x['losses']}L, {x['net_usdt']:+.5f} USDT")
    for side,choice,vals in nominated:
       x=next(r for r in all_records if r['side']==side)
       lines.extend(['',f"## {side} — {x['search_count']} candidate combinations",
           f"Train finalists {x['unique_train_finalists']}; passed positive TRAIN+VALIDATION {x['passed_validation']}."])
       if not choice:lines.append('**No rule achieved positive train + validation under minimum sample requirements.**')
       else:lines.append('SELECTED BEFORE HOLDOUT: '+json.dumps(choice['rule']))
       for item in finalists:
         if item['side']!=side or item['rank']>5:continue
         va=item['outcomes']['100']['validation'];te=item['outcomes']['100']['test']
         lines.append(f"{item['rank']}. {item['rule']['quartiles']} + {item['rule']['pattern']}: validation {va['n']} ({va['wins']}W/{va['losses']}L), {va['net_usdt']:+.5f}; HOLDOUT {te['n']} ({te['wins']}W/{te['losses']}L), {te['net_usdt']:+.5f} USDT")
    if combined is not None:
      lines.extend(['','## Only independently VAL-positive filters combined (no LIVE)'])
      for c in CAPS:
         for p in ('validation','test','all90'):
            v=combined[str(c)][p];lines.append(f"- capital {c}, {p}: {v['n']} entries, {v['wins']}W/{v['losses']}L, {v['net_usdt']:+.5f} USDT ({v['net_pct']:+.3f}% of capital)")
    lines.extend(['','## Interpretation',
       'Repeated pre-entry patterns are observable, but a profitable filter requires both positive validation and holdout.',
       'Inspect original Work replay limitations, unknown real spread and historical Top200; do NOT enable in LIVE from this study.'])
    (HERE/'README.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines),flush=True)

if __name__=='__main__':
    assert len(sys.argv)==2,'usage: python test_sequences.py PATH_TO_EXISTING_SHARDS'
    main(sys.argv[1])
