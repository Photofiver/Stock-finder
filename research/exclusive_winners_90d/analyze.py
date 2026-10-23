#!/usr/bin/env python3
"""READ-ONLY historical audit: find entry-indicator conditions present in wins but absent in losses.
Uses saved LIVE-replay middle-cost candidates; no network, no exchange calls, no LIVE modifications.
2017? No: assert expected 1917 WIN / 3858 LOSS, 5775 closed 500 USDT 90-day portfolio.
"""
import ast,collections,csv,gzip,itertools,json,math,statistics
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
DATA=ROOT/'research/capital_replay_20261023/candidate_parts'
START=1784999700000;DAY=86400000;TRAIN_END=START+60*DAY
VAL_END=START+75*DAY;END=START+90*DAY

def read():
    files=sorted(DATA.glob('candidates_*.csv.gz'))
    assert len(files)==10, f"Expected ten existing candidate gzip files, got {len(files)}"
    rows=[]
    for p in files:
        with gzip.open(p,'rt',newline='') as f:
            for d in csv.DictReader(f):
                if abs(float(d['slippage_per_side'])-.00025)>1e-10: continue
                x={k:d[k] for k in ('inst','side')}
                x.update({k:int(d[k]) for k in ('t','exit_t','earliest_anchor')})
                x.update({k:float(d[k]) for k in ('net','notional','strength','gross','fees')})
                x['closed']=d['closed']=='True'
                x['features']=ast.literal_eval(d['features'])
                rows.append(x)
    rows.sort(key=lambda x:(x['t'],-x['strength'],x['inst']))
    assert len(rows)==5994, f"Unexpected 0.025% scenario candidate count: {len(rows)}"
    return rows

def original_selection(rows,capital=500):
    """Rebuild original Work model's actual 500 USDT sequence of trades, retaining rows."""
    active={};restart={};done=[];real=0.;stopped=False;reject=collections.Counter()
    def settle(now):
        nonlocal real
        for sym,x in list(active.items()):
            if x['exit_t']<=now:
                done.append(x);real+=x['net'];restart[sym]=x['exit_t'];del active[sym]
    for x in rows:
        if not START<=x['t']<END:continue
        settle(x['t'])
        if capital+real<=capital*.9:stopped=True
        if stopped:reject['risk_stop']+=1;continue
        if x['inst'] in active:reject['already_open']+=1;continue
        if x['earliest_anchor']<=restart.get(x['inst'],0):reject['restart_pivots']+=1;continue
        if len(active)>=4:reject['four_slots']+=1;continue
        committed=sum(y['notional'] for y in active.values())
        available=capital+real-committed-sum(y['notional']*.0006 for y in active.values())
        spendable=min(min(capital,capital+real)-committed,available-.5)
        if spendable<5:reject['free_bankroll_below_5']+=1;continue
        active[x['inst']]=x
    settle(END)
    d=[x for x in done if x['closed']]
    d.sort(key=lambda x:(x['t'],-x['strength'],x['inst']))
    wins=sum(x['net']>0 for x in d);losses=sum(x['net']<0 for x in d)
    result=dict(total=len(d),wins=wins,losses=losses,net=sum(x['net'] for x in d),
                rejection=dict(reject))
    assert (len(d),wins,losses)==(5775,1917,3858),result
    return d,result

def split(x):
    if x['t']<TRAIN_END and x['exit_t']<=TRAIN_END:return 'train'
    if TRAIN_END<=x['t']<VAL_END and x['exit_t']<=VAL_END:return 'validation'
    if VAL_END<=x['t']<END and x['exit_t']<=END:return 'test'
    return 'boundary'

def count(bits,win,loss):
    return (bits&win).bit_count(),(bits&loss).bit_count()

def main():
    d,original=original_selection(read())
    fields=list(d[0]['features'])
    for x in d:
        for f in fields:
            if f not in x['features']:raise ValueError(f)
    masks={}
    for i,x in enumerate(d):
        masks[('side',x['side'])]=masks.get(('side',x['side']),0)|(1<<i)
        p=split(x)
        masks[('period',p)]=masks.get(('period',p),0)|(1<<i)
        label='win' if x['net']>0 else 'loss'
        masks[('outcome',label)]=masks.get(('outcome',label),0)|(1<<i)
    def mask(side,period,kind):
        return masks[('side',side)] & masks[('period',period)] & masks[('outcome',kind)]
    selected=[]
    for side in ('LONG','SHORT'):
        scope=masks[('side',side)]
        train=[x for x in d if x['side']==side and split(x)=='train']
        choices=[]
        for feature in fields:
            v=sorted(float(x['features'][feature]) for x in train if x['features'][feature] is not None and math.isfinite(float(x['features'][feature])))
            if len(v)<30:continue
            thresholds={v[int((len(v)-1)*q)] for q in (.05,.10,.20,.35,.50,.65,.80,.90,.95)}
            if min(v)<0<max(v):thresholds.add(0.0)
            for threshold in sorted(thresholds):
                for op in ('>=','<='):
                    b=0
                    for i,x in enumerate(d):
                        if x['side']!=side:continue
                        val=x['features'].get(feature)
                        if val is not None and math.isfinite(float(val)) and ((float(val)>=threshold) if op=='>=' else (float(val)<=threshold)):
                            b|=1<<i
                    trainhits=(b & masks[('period','train')]).bit_count()
                    if trainhits<5:continue
                    if b==scope or b==0:continue
                    choices.append((feature,op,float(threshold),b))
        # Deduplicate atom masks to avoid testing the same decision repeatedly.
        unique={}
        for feat,op,t,b in choices:
            if b not in unique:unique[b]=(feat,op,t,b)
        choices=list(unique.values())
        choices.sort(key=lambda x:(x[0],x[1],x[2]))
        trainwin=mask(side,'train','win');trainloss=mask(side,'train','loss')
        valwin=mask(side,'validation','win');valloss=mask(side,'validation','loss')
        testwin=mask(side,'test','win');testloss=mask(side,'test','loss')
        allwin=scope&masks[('outcome','win');allloss=scope&masks[('outcome','loss')
        atom_counts=[]
        for feat,op,t,b in choices:
            tw,tl=count(b,trainwin,trainloss)
            vw,vl=count(b,valwin,valloss)
            hw,hl=count(b,testwin,testloss)
            atom_counts.append(dict(feature=feat,operator=op,threshold=t,
                training_wins=tw,training_losses=tl,validation_wins=vw,validation_losses=vl,
                test_wins=hw,test_losses=hl,win_rate_train=tw/(tw+tl) if tw+tl else None,
                win_rate_validation=vw/(vw+vl) if vw+vl else None))
        # Thresholds were fixed from TRAIN. Crosscheck without selecting on holdout.
        single=sorted(atom_counts,key=lambda r:(r['validation_wins']-r['validation_losses'],r['training_wins']-r['training_losses']),reverse=True)[:12]
        # Deliberately search 1-3-way intersections of DIFFERENT features; 4-way
        # only expand train-strong 3-way finalists to keep compute bounded.
        # Bitmasks make tests exact over all 5775 actual selected positions.
        full_exclusive=[]
        training_exclusive=[]
        validation_winner_only=[]
        best_train_gain=[]
        evaluated=collections.Counter()
        all_so_far=[]
        def record(ids,b):
            n=len(ids);evaluated[str(n)]+=1
            tw,tl=count(b,trainwin,trainloss)
            vw,vl=count(b,valwin,valloss)
            hw,hl=count(b,testwin,testloss)
            aw,al=count(b,allwin,allloss)
            atoms=[[choices[z][0],choices[z][1],round(choices[z][2],10)] for z in ids]
            row=dict(rule=atoms,train=[tw,tl],validation=[vw,vl],test=[hw,hl],all90=[aw,al],
                     training_net=round(sum(d[i]['net'] for i in range(len(d)) if (b>>i)&1 and d[i]['side']==side and split(d[i])=='train'),6) if tw>=6 and n<=2 else None)
            if aw>=2 and al==0:full_exclusive.append(row)
            if tw>=8 and tl==0:training_exclusive.append(row)
            if tw>=10 and tl<=1 and vw>=3 and vl==0:
                validation_winner_only.append(row)
            if tw>=12 and tw>2*tl and n==3:
                best_train_gain.append((tw-2*tl,ids,b))
            return tw,tl,vw,vl
        for i,a in enumerate(choices):
            tw,tl,vw,vl=record((i,),a[3])
        for i,a in enumerate(choices):
            for j in range(i+1,len(choices)):
                z=choices[j]
                if a[0]==z[0]:continue
                b=a[3]&z[3]
                tw,tl=count(b,trainwin,trainloss)
                if tw<2:continue
                record((i,j),b)
                if tw+tl<5:continue
                for k in range(j+1,len(choices)):
                    x=choices[k]
                    if x[0] in (a[0],z[0]):continue
                    bk=b&x[3]
                    tw3,tl3=count(bk,trainwin,trainloss)
                    if tw3<2:continue
                    record((i,j,k),bk)
        # 4-condition deepening is explicitly bounded to the top 100 three-way
        # training masks; holdout is NEVER involved in selection.
        best_train_gain.sort(key=lambda r:(r[0],-len(r[1])),reverse=True)
        candidates=best_train_gain[:100]
        for score,ids,b in candidates:
            used={choices[z][0] for z in ids}
            for q in range(ids[-1]+1,len(choices)):
                if choices[q][0] in used:continue
                bk=b&choices[q][3]
                if (bk&trainwin).bit_count()<2:continue
                record(ids+(q,),bk)
        # Freeze selection on training+validation, without test outcomes.
        by_val=sorted(validation_winner_only,
            key=lambda r:(r['validation'][0],r['train'][0],-len(r['rule'])),reverse=True)
        frozen=by_val[:10]
        # "Zero losses over all 90 days" is only retrospective; do NOT call validated.
        posthoc=sorted(full_exclusive,key=lambda r:(r['all90'][0],-len(r['rule'])),reverse=True)[:20]
        train_only=sorted(training_exclusive,key=lambda r:r['train'][0],reverse=True)[:15]
        best_positive=sorted(atom_counts,
            key=lambda r:(r['validation_wins']-r['validation_losses'],r['training_wins']-r['training_losses']),reverse=True)[:10]
        selected.append(dict(side=side,base=dict(train=count(scope,trainwin,trainloss),
                        validation=count(scope,valwin,valloss),test=count(scope,testwin,testloss),
                        all90=count(scope,allwin,allloss)),
            unique_single_atoms=len(choices),tested=dict(evaluated),
            number_of_all90_zero_loss_rules=len(full_exclusive),
            number_of_train_only_zero_loss_rules=len(training_exclusive),
            number_of_train_validation_zero_loss_rules=len(validation_winner_only),
            top_posthoc_90d_zero_loss=posthoc,
            top_train_only_zero_loss=train_only,validation_selected=frozen,
            single_feature_ranked=single,best_nonexclusive_singles=best_positive))
        print(side, 'entries',int(scope.bit_count()),'atom count',len(choices),
              'tested',dict(evaluated),'all90 exclusive',len(full_exclusive),
              'training exclusive',len(training_exclusive),'validated',len(validation_winner_only),flush=True)
    report=dict(status='READ_ONLY_LIVE_UNCHANGED',run_source=39994070142,
         frozen_replay='research/live-capital-replay-20261023',
         capital=500,commission_per_side_pct=.06,assumed_adverse_execution_per_side_pct=.025,
         actual_90d_portfolio=original,selection=selected,
         method='Train-period-derived quantile indicator thresholds and intersections of 1-3 different features, plus up to 100 train-strong three-condition sets expanded to 4, evaluated on 5775 closed baseline 500 USDT portfolio trades. Zero-loss all90 search is POSTHOC only; frozen train+validation decisions evaluated on last 15d holdout.',
         warnings=['Selected 500 USDT replay trades not exact real BloFin orders; historical TOP200 missing, fee/slide assumptions.', 'Only 14 pre-entry indicator features, not all OHLCV sequences or combinations.','Filters optimized on 90d hindsight cannot be called predictive; holdout is independent only for preselected rules.','No changes to LIVE, positions, GitHub main, exchange.'])
    HERE.mkdir(exist_ok=True)
    (HERE/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    lines=['# 90-day BloFin winner-exclusive characteristics — research only',
        '**LIVE unchanged. Historical strategy replay, not actual broker fills.**',
        f"Portfolio 500 USDT, 1x, <=5 USDT/position, up to 4: **{original['wins']} winners, {original['losses']} losers, {original['total']} trades**.",
        'All after 0.06% fees and assumed 0.025% adverse execution each side.',
        'Train 60 days, validation 15 days, independent holdout 15 days. Train-derived thresholds.',
        '', 'Question: Is there a pre-entry condition all winners share and all losers lack?',
        'Any zero-loss pattern found by looking at ALL 90 days is a hindsight description only; it cannot be assumed to recur.',]
    for s in selected:
      lines.extend(['',f"## {s['side']}",f"Base train/val/test/all90 [W,L]: {s['base']}",
         f"Unique atomic filters {s['unique_single_atoms']}, tested intersections: {s['tested']}",
         f"Exclusively winners retrospectively on all90: {s['number_of_all90_zero_loss_rules']}",
         f"Train winner-only (>=8): {s['number_of_train_only_zero_loss_rules']}",
         f"TRAIN+VALIDATION winner-only (>=10W train and >=3W validation, no validation losses): {s['number_of_train_validation_zero_loss_rules']}",
         '### Highest coverage hindsight zero-loss patterns (NOT validated)'])
      for r in s['top_posthoc_90d_zero_loss'][:8]:
        lines.append(f"- {r['rule']}: ALL90 {r['all90']} [W,L], TRAIN {r['train']}, VALID {r['validation']}, TEST {r['test']}")
      lines.append('### Validation-selected winner-only patterns, holdout measured without reranking')
      for r in s['validation_selected'][:6]:
        lines.append(f"- {r['rule']}: TRAIN {r['train']}, VALID {r['validation']}, HOLDOUT {r['test']}, ALL90 {r['all90']}")
      lines.append('### Most favorable single predicates by validation wins-minus-losses (exploratory)')
      for r in s['single_feature_ranked'][:8]:
        lines.append(f"- {r['feature']} {r['operator']} {r['threshold']:.5g}: TRAIN {r['training_wins']}/{r['training_losses']} VALID {r['validation_wins']}/{r['validation_losses']} TEST {r['test_wins']}/{r['test_losses']}")
    lines+=['','## Decision',
        'Only a rule with meaningful sample size and no loss in independent holdout could be called validated within this specific dataset, and even that is not a guarantee of future profitability.',
        'Do not enable winner-exclusive hindsight combinations in LIVE without forward paper testing.']
    (HERE/'README.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines[:26]),flush=True)

if __name__=='__main__':main()
