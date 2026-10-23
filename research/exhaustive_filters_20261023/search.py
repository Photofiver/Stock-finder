#!/usr/bin/env python3
"""Offline, finite, resumable entry-filter research; never imports LIVE code.
Only Python's standard library. Read README.md before interpreting results.
"""
import functools
import argparse, bisect, concurrent.futures, hashlib, json, math, os, sqlite3, time, fcntl
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

BASE_SHA = 'fd6500af847bef6ad48fc7cdc3ad6f63da72215c'
UK = ZoneInfo('Europe/London')
# Metadata/identifiers and information recorded after the decision are not predictors.
EXCLUDE = {'inst','strategy','indicator_timeframe','timeframe','rule','reason','status',
 'indicator_status','indicator_error','sr_diagnostic_error','quote_source','signal_label',
 'actual_fill_adverse_gap_pct','captured_ms','entry_chart','exit_chart'}

def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def flatten(obj, prefix='', excluded=None):
    out = {}
    if isinstance(obj,dict):
        for key,val in sorted(obj.items()):
            p = prefix+'.'+key if prefix else key
            if key in EXCLUDE or ((key.endswith('_ms') or key.endswith('_ts')) and key!='quote_age_ms'):
                if excluded is not None: excluded.add(p)
                continue
            out.update(flatten(val,p,excluded))
    elif isinstance(obj,list):
        for i,val in enumerate(obj): out.update(flatten(val,f'{prefix}[{i}]',excluded))
    elif isinstance(obj,bool): out[prefix] = int(obj)
    elif isinstance(obj,(int,float)) and math.isfinite(obj): out[prefix] = float(obj)
    elif isinstance(obj,str):
        try:
            val=float(obj)
            if math.isfinite(val): out[prefix]=val
        except ValueError:
            # Include categorical indicator states, including side, but no free text.
            if len(obj)<=80: out[prefix]=obj
            elif excluded is not None: excluded.add(prefix)
    return out

def check_times(obj, signal):
    if isinstance(obj,dict):
        for key,val in obj.items():
            if key=='confirmed_close_ms' and int(val)>signal: return False
            if not check_times(val,signal): return False
    if isinstance(obj,list): return all(check_times(x,signal) for x in obj)
    return True

def prepare(state_path, out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    raw=Path(state_path).read_bytes();state=json.loads(raw)
    rows=[];rejected=[];other=[];excluded=set();seen=set()
    for t in state['trade_history']:
        snap=t.get('signal_snapshot') or {}
        ident=f"{t.get('history_id')}:{t.get('inst')}:{t.get('opened_ms')}:{t.get('closed_ms')}"
        if snap.get('strategy')!='PIVOT_TUNNEL_5M':
            other.append({'id':ident,'strategy':t.get('strategy'),'reason':'different historical strategy; not pooled with tunnel outcomes'})
            continue
        try:
            opened=int(t['opened_ms']);closed=int(t['closed_ms']);signal=int(snap['signal_close_ms'])
            assert 0<signal<=opened<closed and check_times(snap,signal),'invalid chronological provenance'
            assert int(snap.get('indicator_signal_close_ms',signal))<=opened,'future indicators'
            assert ident not in seen,'duplicate record'
            notional=float(t['notional_usdt']);net=float(t['net_pnl_usdt'])
            gross=float(t['gross_pnl_usdt']);fee=float(t['fee_usdt'])
            assert all(math.isfinite(x) for x in (notional,net,gross,fee)) and notional>0,'invalid outcome'
            assert abs(net-gross-fee)<1e-7,'inconsistent net/gross/fee'
            features=flatten(snap,excluded=excluded)
            price=float(snap['close'])
            def derived(name,a,b,scale=1):
                if isinstance(a,(int,float)) and isinstance(b,(int,float)) and b:
                    features['derived.'+name]=scale*a/b
            derived('volume_vs_ma20',snap.get('signal_volume'),snap.get('signal_mean20_prev'))
            for key in ('sma10','sma20'):
                if snap.get(key): features['derived.distance_'+key+'_pct']=100*(price/float(snap[key])-1)
            for a,b,name in [('stoch_k','stoch_d','stoch_k_minus_d'),('macd_hist','macd_hist_previous','macd_change'),('rsi14','rsi14_previous','rsi_change')]:
                if snap.get(a) is not None and snap.get(b) is not None: features['derived.'+name]=float(snap[a])-float(snap[b])
            tunnel=snap.get('volume_price_tunnel') or {}
            lo=tunnel.get('lower_price');hi=tunnel.get('upper_price')
            if isinstance(lo,(int,float)) and isinstance(hi,(int,float)) and hi>lo:
                features['derived.tunnel_width_pct']=100*(hi-lo)/price
                features['derived.tunnel_position']=(price-lo)/(hi-lo)
                reward,risk=(hi-price,price-lo) if t['side']=='LONG' else (price-lo,hi-price)
                if risk>0: features['derived.signal_line_reward_risk']=reward/risk
            day=datetime.fromtimestamp(opened/1000,UK).date().isoformat()
            # Freeze entire calendar periods before looking for rules. Purge crossing trades.
            split='train' if day<'2026-10-22' else 'validation' if day<'2026-10-23' else 'test'
            close_day=datetime.fromtimestamp(closed/1000,UK).date().isoformat()
            end_day='2026-10-22' if split=='train' else '2026-10-23' if split=='validation' else None
            if end_day and close_day>=end_day: split='purged_boundary'
            rows.append({'id':ident,'opened_ms':opened,'closed_ms':closed,'signal_close_ms':signal,
              'day_uk':day,'split':split,'inst':t['inst'],'side':t['side'],'code_commit':t.get('code_commit'),
              'notional_original':notional,'net_rate':net/notional,'fee_rate':fee/notional,
              'gross_rate':gross/notional,'features':features})
            seen.add(ident)
        except (KeyError,ValueError,TypeError,AssertionError,ZeroDivisionError) as e:
            rejected.append({'id':ident,'reason':str(e)})
    rows.sort(key=lambda x:(x['opened_ms'],x['id']))
    train=[r for r in rows if r['split']=='train']
    if not train: raise ValueError('No training records')
    fields=sorted({k for r in rows for k in r['features']})
    specs=[]
    for key in fields:
        values=[r['features'][key] for r in train if key in r['features']]
        numeric=all(isinstance(v,(int,float)) for v in values)
        if numeric:
            thresholds=sorted(set(values));m=2*len(thresholds)+1
            # option 0 disabled, 1 missing, 2... each contiguous interval of bins.
            specs.append({'name':key,'kind':'numeric','values':thresholds,'radix':2+m*(m+1)//2})
        else:
            categories=sorted(set(str(v) for v in values))
            specs.append({'name':key,'kind':'category','values':categories,'radix':2**len(categories)+1})
    total=math.prod(s['radix'] for s in specs)-1
    manifest={'version':1,'source_commit':BASE_SHA,'source_sha256':hashlib.sha256(raw).hexdigest(),
      'historical_records_in_state':len(state['trade_history']),'eligible_tunnel_records':len(rows),
      'split_counts':{k:sum(r['split']==k for r in rows) for k in ['train','validation','test','purged_boundary']},
      'excluded_records':rejected,'other_strategy_records':other,'excluded_snapshot_paths':sorted(excluded),
      'features':specs,'total_combinations':str(total),'rows_sha256':digest(rows),
      'scope':'Conjunction of zero or one grid predicate per snapshot feature; at least one active feature. All depths 1..F. Frozen training thresholds only.',
      'limitations':['Observed executed tunnel trades only, not a full replay of all rejected market opportunities.',
       'Historical tunnel versions have different exits and settings; outcomes are not a replay of one current code version.',
       'Saved final TP/SL excluded; actual initial broker TP/SL not reconstructed from git history.',
       'Raw scan archives, older git states and Actions artifacts have not been fully ingested.',
       'No arbitrary new indicator periods, arithmetic formulas or arbitrary Boolean programs are claimed.',
       'Shared 20 USDT model scales historical fill returns; lot sizes, funding and counterfactual slippage are not reconstructed.',
       'Validation/test periods were discussed in earlier conversations; chronological holdout here is not a pristine prospective test.']}
    manifest['manifest_sha256']=digest(manifest)
    (out/'rows.json').write_text(json.dumps(rows,separators=(',',':'),allow_nan=False))
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,allow_nan=False))
    print(json.dumps({k:manifest[k] for k in ['eligible_tunnel_records','split_counts','total_combinations']}))

class Space:
    def __init__(self,manifest,rows):
        self.specs=manifest['features'];self.rows=rows;self.allmask=(1<<len(rows))-1
        n=len(self.specs);self.weights=[[0]*(n+1) for _ in range(n+1)];self.weights[n][0]=1
        for i in range(n-1,-1,-1):
            self.weights[i][0]=1
            for k in range(1,n-i+1):
                self.weights[i][k]=self.weights[i+1][k]+(self.specs[i]['radix']-1)*self.weights[i+1][k-1]
        self.total=sum(self.weights[0][1:]);self.tables=[]
        for spec in self.specs:
            missing=0;vals=spec['values'];bins=[0]*(2*len(vals)+1 if spec['kind']=='numeric' else len(vals))
            for j,row in enumerate(rows):
                v=row['features'].get(spec['name']);bit=1<<j
                if v is None: missing|=bit;continue
                if spec['kind']=='numeric':
                    if not isinstance(v,(float,int)):continue
                    p=bisect.bisect_left(vals,v);b=2*p+int(p<len(vals) and vals[p]==v)
                else:
                    if str(v) not in vals:continue
                    b=vals.index(str(v))
                bins[b]|=bit
            prefix=[0]
            for b in bins: prefix.append(prefix[-1]|b)
            self.tables.append((missing,bins,prefix))
    def unrank(self,index):
        if not 0<=index<self.total:raise ValueError('index outside space')
        k=1
        while index>=self.weights[0][k]:index-=self.weights[0][k];k+=1
        choices=[]
        for i,spec in enumerate(self.specs):
            absent=self.weights[i+1][k]
            if index<absent:choices.append(0)
            else:
                index-=absent;ways=self.weights[i+1][k-1]
                q,index=divmod(index,ways);choices.append(q+1);k-=1
        assert k==0 and index==0
        return choices
    def interval(self,spec,option):
        m=2*len(spec['values'])+1;offset=option-2;a=0
        # Row-major upper triangle; binary search finds the interval's first bin.
        lo,hi=0,m
        while lo<hi:
            mid=(lo+hi+1)//2
            if mid*(2*m-mid+1)//2<=offset:lo=mid
            else:hi=mid-1
        a=lo;b=a+offset-a*(2*m-a+1)//2
        return a,b
    def optionmask(self,i,option):
        missing,bins,prefix=self.tables[i];spec=self.specs[i]
        if option==0:return self.allmask
        if option==1:return missing
        if spec['kind']=='numeric':
            a,b=self.interval(spec,option);return prefix[b+1]^prefix[a]
        subset=option-1;mask=0
        for j,bit in enumerate(bins):
            if subset>>j&1:mask|=bit
        return mask
    def mask(self,choices):
        mask=self.allmask
        for i,option in enumerate(choices):
            if option: mask&=self.optionmask(i,option)
            if not mask:break
        return mask
    def describe(self,choices):
        rules=[]
        for i,option in enumerate(choices):
            if not option:continue
            s=self.specs[i];name=s['name'];vals=s['values']
            if option==1:rules.append({'feature':name,'operator':'is_missing'});continue
            if s['kind']=='category':
                rules.append({'feature':name,'in':[v for j,v in enumerate(vals) if (option-1)>>j&1]});continue
            a,b=self.interval(s,option);m=2*len(vals)+1
            lower=None if a==0 else vals[(a-1)//2]
            upper=None if b==m-1 else vals[b//2]
            rules.append({'feature':name,'lower':lower,'lower_inclusive':bool(a%2),
                          'upper':upper,'upper_inclusive':bool(b%2)})
        return rules

def portfolio(rows,mask,split):
    equity=20.;busy=-1;wins=losses=flat=0;fees=0.;executed=[];capital_skips=0
    for i,r in enumerate(rows):
        if not(mask>>i&1) or r['split']!=split:continue
        if r['opened_ms']<busy:capital_skips+=1;continue
        if equity<=0:continue
        stake=min(20.,equity);net=stake*r['net_rate'];equity+=net
        fees+=stake*r['fee_rate'];busy=r['closed_ms']
        wins+=net>1e-12;losses+=net< -1e-12;flat+=abs(net)<=1e-12
        executed.append(r['id'])
    return {'wins':wins,'losses':losses,'flat':flat,'net_usdt':equity-20.,
      'return_pct':(equity/20.-1)*100,'fees_signed_usdt':fees,'capital_skips':capital_skips,'trade_ids':executed}

def objective(result):
    # Zero trades cannot defeat a profitable zero-loss rule. If none exists, minimize losses.
    return (int(result['losses']==0 and result['wins']>0),-result['losses'],result['wins'])

WORK=None
TRAIN_MASK=0

@functools.lru_cache(maxsize=8192)
def training_result(mask):
    return portfolio(WORK.rows,mask,'train')

def init_worker(manifest,rows):
    global WORK,TRAIN_MASK
    WORK=Space(manifest,rows)
    TRAIN_MASK=sum(1<<i for i,r in enumerate(rows) if r['split']=='train')
    training_result.cache_clear()

def batch(start,end):
    results=[]
    for index in range(start,end):
        choices=WORK.unrank(index);mask=WORK.mask(choices);r=training_result(mask&TRAIN_MASK)
        results.append((str(index),*objective(r),str(mask),json.dumps(r,separators=(',',':'))))
    return results

def load(folder):
    p=Path(folder);m=json.loads((p/'manifest.json').read_text());r=json.loads((p/'rows.json').read_text())
    expected=m['manifest_sha256'];copy=dict(m);del copy['manifest_sha256']
    if digest(copy)!=expected or digest(r)!=m['rows_sha256']:raise ValueError('Input hash mismatch')
    return m,r

def db_open(folder,manifest):
    con=sqlite3.connect(str(Path(folder)/'progress.sqlite'))
    con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA synchronous=FULL')
    con.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS best (id TEXT PRIMARY KEY, feasible INTEGER, neg_losses INTEGER, wins INTEGER, mask TEXT, train_json TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS ledger (start TEXT PRIMARY KEY, stop TEXT, n INTEGER, result_sha256 TEXT)')
    old=con.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    if old and old[0]!=manifest['manifest_sha256']:raise ValueError('Checkpoint belongs to different inputs')
    con.execute("INSERT OR IGNORE INTO meta VALUES ('manifest',?)",(manifest['manifest_sha256'],));con.commit()
    checkpoint=Path(folder)/'checkpoint.json'
    if not old and checkpoint.exists():
        cp=json.loads(checkpoint.read_text());signature=cp.pop('sha256')
        if digest(cp)!=signature or cp['manifest_sha256']!=manifest['manifest_sha256']:
            raise ValueError('Invalid portable checkpoint')
        with con:
            con.executemany('INSERT INTO ledger VALUES (?,?,?,?)',cp['ledger'])
            con.executemany('INSERT INTO best VALUES (?,?,?,?,?,?)',cp['best'])
            con.execute("INSERT OR REPLACE INTO meta VALUES ('next_index',?)",(cp['next_index'],))
    return con

def commit_batch(con,start,end,result):
    assert [int(r[0]) for r in result]==list(range(start,end)), 'Worker omitted or duplicated an index'
    score=max(tuple(r[1:4]) for r in result)
    old=con.execute('SELECT feasible,neg_losses,wins FROM best LIMIT 1').fetchone()
    with con:
        con.execute('INSERT INTO ledger VALUES (?,?,?,?)',(str(start),str(end),end-start,digest(result)))
        if old is None or score>old:
            con.execute('DELETE FROM best')
        if old is None or score>=old:
            con.executemany('INSERT INTO best VALUES (?,?,?,?,?,?)',[r for r in result if tuple(r[1:4])==score])
        con.execute("INSERT OR REPLACE INTO meta VALUES ('next_index',?)",(str(end),))

def run(folder,workers,seconds,chunk,max_combinations=None):
    if workers<1 or chunk<1 or seconds<0:raise ValueError('Invalid execution settings')
    lock=open(Path(folder)/'run.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    manifest,rows=load(folder);con=db_open(folder,manifest);total=int(manifest['total_combinations'])
    cursor=int((con.execute("SELECT value FROM meta WHERE key='next_index'").fetchone() or ['0'])[0])
    stop=min(total,cursor+max_combinations) if max_combinations is not None else total
    deadline=time.monotonic()+seconds;last_log=0
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers,initializer=init_worker,initargs=(manifest,rows)) as pool:
        while cursor<stop and time.monotonic()<deadline:
            ranges=[(a,min(a+chunk,stop)) for a in range(cursor,min(cursor+workers*chunk,stop),chunk)]
            futures=[pool.submit(batch,a,b) for a,b in ranges]
            for (a,b),f in zip(ranges,futures):
                commit_batch(con,a,b,f.result());cursor=b
            if time.monotonic()-last_log>=5:
                print(json.dumps({'committed':cursor}),flush=True);last_log=time.monotonic()
    con.execute('PRAGMA wal_checkpoint(TRUNCATE)');con.close()
    report(folder)
    lock.close()

def report(folder):
    m,rows=load(folder);space=Space(m,rows);con=db_open(folder,m)
    ledger=sorted([(int(a),int(b),n,h) for a,b,n,h in con.execute('SELECT * FROM ledger')]);count=sum(r[2] for r in ledger);total=space.total
    cursor=int((con.execute("SELECT value FROM meta WHERE key='next_index'").fetchone() or ['0'])[0])
    # Audit every persisted ID; primary key proves uniqueness, this proves no gaps/out-of-range IDs.
    expected=0;contiguous=True
    for a,b,n,h in ledger:
        contiguous=contiguous and a==expected and b-a==n and 0<=a<b<=total and len(h)==64
        expected=b
    contiguous=contiguous and expected==cursor==count
    complete=contiguous and count==total
    with localcontext() as ctx:
        ctx.prec=12;coverage=str(Decimal(count)*100/Decimal(total))
    best=con.execute('SELECT feasible,neg_losses,wins FROM best ORDER BY feasible DESC,neg_losses DESC,wins DESC LIMIT 1').fetchone()
    tied=[]
    if best:
        for index,mask,tr in con.execute('SELECT id,mask,train_json FROM best WHERE feasible=? AND neg_losses=? AND wins=?',best):
            tied.append((int(index),int(mask),json.loads(tr)))
    # Select a canonical representative USING TRAINING ONLY; holdouts never rank rules.
    tied.sort(key=lambda x:(len(space.describe(space.unrank(x[0]))),x[0]))
    champion=None
    if tied:
        index,mask,tr=tied[0]
        champion={'index':str(index),'rules':space.describe(space.unrank(index)),'train':tr,
          'validation':portfolio(rows,mask,'validation'),'test':portfolio(rows,mask,'test')}
    # Every tied rule receives separate holdout scores, without selecting the holdout winner.
    with (Path(folder)/'best_ties.jsonl').open('w') as f:
        for index,mask,tr in tied:
            f.write(json.dumps({'index':str(index),'train':tr,'validation':portfolio(rows,mask,'validation'),
              'test':portfolio(rows,mask,'test')},separators=(',',':'))+'\n')
    summary={'complete':complete,'scope':m['scope'],'features':len(m['features']),
      'total_combinations':str(total),'actually_evaluated':count,'coverage_pct':coverage,
      'contiguous_unique_valid_indices':contiguous,'none_omitted':True if complete else False,
      'next_index':str(cursor),'max_conditions':len(m['features']),'best_tie_count':len(tied),
      'best_so_far':champion,'split_counts':m['split_counts'],'limitations':m['limitations'],
      'source_commit':m['source_commit'],'manifest_sha256':m['manifest_sha256'],
      'baseline':{s:portfolio(rows,space.allmask,s) for s in ['train','validation','test']}}
    used=0;summary['evaluated_by_condition_count']={}
    for k in range(1,len(space.specs)+1):
        size=space.weights[0][k]
        summary['evaluated_by_condition_count'][str(k)]={'total':str(size),'evaluated':str(max(0,min(count-used,size)) if contiguous else 0)}
        used+=size
    summary['engine_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (Path(folder)/'report.json').write_text(json.dumps(summary,indent=2))
    checkpoint={'manifest_sha256':m['manifest_sha256'],'next_index':str(cursor),
       'ledger':con.execute('SELECT * FROM ledger').fetchall(),'best':con.execute('SELECT * FROM best').fetchall()}
    checkpoint['sha256']=digest(checkpoint)
    tmp=Path(folder)/'checkpoint.json.tmp'
    tmp.write_text(json.dumps(checkpoint,separators=(',',':')))
    os.replace(tmp,Path(folder)/'checkpoint.json')
    print(json.dumps({k:summary[k] for k in ['complete','features','actually_evaluated','coverage_pct','best_tie_count']}))
    con.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('prepare');q.add_argument('--state',required=True);q.add_argument('--out',required=True)
    q=sub.add_parser('run');q.add_argument('--out',required=True);q.add_argument('--workers',type=int,default=2);q.add_argument('--seconds',type=float,default=300);q.add_argument('--chunk',type=int,default=250);q.add_argument('--max-combinations',type=int)
    q=sub.add_parser('report');q.add_argument('--out',required=True)
    a=p.parse_args()
    if a.command=='prepare':prepare(a.state,a.out)
    elif a.command=='run':run(a.out,a.workers,a.seconds,a.chunk,a.max_combinations)
    else:report(a.out)
