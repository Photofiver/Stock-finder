#!/usr/bin/env python3
"""BloFin 90d / 200-symbol historical research. READ ONLY. NO public or private API calls.
A reproducible multi-strategy *backtest*, not a replay of the existing moving-tunnel LIVE bot.
90d 5m data from run 39994070142; fixed as-of-2026-10-23 universe (survivorship risk).
Selection: first 60 days train, next 15 validate, LAST 15 days final untouched holdout.
Trades enter at NEXT bar open; SL-first ambiguity; slippage + 2 taker fees deducted.
One position globally; strongest signal vol/MA20 first, shared <=20 USDT capital.
"""
from __future__ import annotations
import argparse
import gzip
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
import numpy as np
import pandas as pd

FIVE=300000
FAMILY=("FLIP","DOMINANT_VOL","BREAKOUT12","TREND_3","MA10_CROSS","RSI_REBOUND")
SIDES=("BOTH","LONG","SHORT")
BODIES=(0.45,0.75,1.20,2.0)
VOLUMES=(1.75,3.0,5.0)
ADXS=(20,30,40)
EXITS=(
    (0.0055,0.0050,6),
    (0.0080,0.0050,6),
    (0.0150,0.0075,12),
    (0.0250,0.0100,24),
    (0.0350,0.0150,48),
    (0.0200,0.0050,12),
)
MAX_HOLD=48
FEE_PCT=0.0012  # two taker orders 0.06% each (rate assumed, not verified per fill)
SLIPPAGE_PCT=0.0010  # conservative adverse total, NOT historical measured spread
TOTAL_COST=FEE_PCT+SLIPPAGE_PCT
START_BALANCE=20.0
CASH_RESERVE=0.50
MAX_NOTIONAL=19.50
MIN_ENTRIES_TRAIN=25
MIN_ENTRIES_VALID=10
MIN_ACTIVE_DAYS_TRAIN=10

def utc(ms):
    return datetime.fromtimestamp(int(ms)/1000,timezone.utc).isoformat()

def wilder_adx(h,l,c,n=14):
    up=h.diff();down=-l.diff()
    pdm=up.where((up>down)&(up>0),0.)
    mdm=down.where((down>up)&(down>0),0.)
    tr=pd.concat((h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()),axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/n,adjust=False).mean().replace(0,np.nan)
    plus=100*pdm.ewm(alpha=1/n,adjust=False).mean()/atr
    minus=100*mdm.ewm(alpha=1/n,adjust=False).mean()/atr
    dx=100*(plus-minus).abs()/(plus+minus).replace(0,np.nan)
    return dx.ewm(alpha=1/n,adjust=False).mean().to_numpy()

def calc_rsi(c,n=14):
    diff=c.diff()
    u=diff.clip(lower=0).ewm(alpha=1/n,adjust=False).mean()
    d=(-diff).clip(lower=0).ewm(alpha=1/n,adjust=False).mean()
    rsi=100-100/(1+u/d.replace(0,np.nan))
    return rsi.fillna(50).to_numpy()

def outcome_batch(ts,o,h,l,c,inds,side):
    """One exit result per predefined TP/SL/hold combination. All original candle data valid."""
    if len(inds)==0:
        return np.empty((0,len(EXITS))),np.empty((0,len(EXITS)),np.int64)
    ent=o[inds+1]
    assert (ent>0).all()
    m=len(inds)
    pnls=np.zeros((m,len(EXITS)),dtype=np.float32)
    end=np.zeros((m,len(EXITS)),dtype=np.int64)
    for k,(tp,sl,hold) in enumerate(EXITS):
        target=ent*(1+side*tp)
        stop=ent*(1-side*sl)
        active=np.ones(m,dtype=bool)
        close_ret=side*(c[inds+hold]/ent-1)
        out=close_ret.copy()
        exit_ts=ts[inds+hold]+FIVE
        for shift in range(1,hold+1):
            ii=np.where(active)[0]
            if len(ii)==0:break
            bar=inds[ii]+shift
            opening=o[bar]; high=h[bar];low=l[bar]
            # SL checked before TP in the SAME 5m bar: adverse ambiguity.
            if side==1:
                stop_hit=low<=stop[ii]
                stop_fill=np.minimum(opening,stop[ii]) # adverse gap through stop
                target_hit=high>=target[ii]
            else:
                stop_hit=high>=stop[ii]
                stop_fill=np.maximum(opening,stop[ii])
                target_hit=low<=target[ii]
            hit=stop_hit|target_hit
            if np.any(hit):
                ids=ii[hit]
                prices=np.where(stop_hit[hit],stop_fill[hit],target[ids])
                out[ids]=side*(prices/ent[ids]-1)
                exit_ts[ids]=ts[bar[hit]]+FIVE
                active[ids]=False
        pnls[:,k]=out-TOTAL_COST
        end[:,k]=exit_ts
    return pnls,end

def build_events(folder, cutoff):
    frames=[]
    count=0
    stats={'symbols':0,'candles':0,'signals':0,'invalid_candles':0,'symbols_without_data':0}
    files=sorted(Path(folder).glob("*.json.gz"))
    for p in files:
        try:
            with gzip.open(p,'rt',encoding='utf-8') as fh:doc=json.load(fh)
            if doc.get('bar')!='5m' or not doc.get('instId'):continue
            rr=doc.get('rows') or []
            if len(rr)<500:stats['symbols_without_data']+=1;continue
            ar=np.asarray([[float(v) for v in row[:6]] for row in rr],dtype=np.float64)
            ts=ar[:,0].astype(np.int64)
            o,h,l,c,v=(ar[:,j] for j in range(1,6))
            if not (np.diff(ts)>0).all() or np.any(o<=0) or np.any(h<np.maximum(o,c)) or np.any(l>np.minimum(o,c)) or np.any(v<0):
                stats['invalid_candles']+=1;continue
            close=pd.Series(c);volume=pd.Series(v);high=pd.Series(h);low=pd.Series(l)
            sma10=close.rolling(10).mean().to_numpy()
            sma20=close.rolling(20).mean().to_numpy()
            prevvol20=volume.shift(1).rolling(20).mean().to_numpy()
            prevprevvol20=volume.shift(2).rolling(20).mean().to_numpy()
            vratio=v/np.where(prevvol20>0,prevvol20,np.nan)
            bodies=np.abs(c-o)/o*100
            dirn=np.sign(c-o).astype(np.int8)
            adx=wilder_adx(high,low,close)
            rsi=calc_rsi(close)
            hi12=high.shift(1).rolling(12).max().to_numpy()
            lo12=low.shift(1).rolling(12).min().to_numpy()
            ma10prev=np.roll(sma10,1);ma10prev[0]=np.nan
            pastgreen=np.full(len(ts),np.nan);pastred=np.full(len(ts),np.nan)
            pg=pr=np.nan
            for i in range(len(ts)):
                pastgreen[i]=pg;pastred[i]=pr
                if dirn[i]==1:pg=v[i]
                elif dirn[i]==-1:pr=v[i]
            opposite=np.where(dirn==1,pastred,pastgreen)
            admissible=np.isfinite(adx)&(adx>=20)&(np.isfinite(vratio))&(vratio>=1.75)&(bodies>=0.45)
            admissible[:300]=False
            admissible[-MAX_HOLD-1:]=False
            # Require 24 uninterrupted future candles to avoid inventing unobserved fills.
            gap=np.r_[0,np.cumsum((np.diff(ts)!=FIVE).astype(int))]
            ii=np.arange(0,len(ts)-MAX_HOLD-1)
            clean=(gap[ii+MAX_HOLD]-gap[ii]==0)
            admissible[ii] &= clean
            fixed=np.isfinite(opposite)&(opposite>0)
            changed=(dirn!=np.roll(dirn,1))&(dirn!=0)&(np.roll(dirn,1)!=0)
            changed[0]=False
            prevqualified=np.roll(v<=prevvol20,1);prevqualified[0]=False
            families={
              'FLIP':changed & (v>np.roll(v,1)) & (v<=prevvol20)&prevqualified,
              'DOMINANT_VOL':fixed & (v>opposite*1.15) & (dirn!=0),
              'BREAKOUT12':((dirn==1)&(c>hi12))|((dirn==-1)&(c<lo12)),
              'TREND_3':(dirn!=0)&(dirn==np.roll(dirn,1))&(dirn==np.roll(dirn,2))&
                 (((dirn==1)&(sma10>sma20)&(c>sma10)) | ((dirn==-1)&(sma10<sma20)&(c<sma10))),
              'MA10_CROSS':((dirn==1)&(c>sma10)&(np.roll(c,1)<=ma10prev))|
                           ((dirn==-1)&(c<sma10)&(np.roll(c,1)>=ma10prev)),
              'RSI_REBOUND':((dirn==1)&(rsi>=35)&(np.roll(rsi,1)<35))|
                            ((dirn==-1)&(rsi<=65)&(np.roll(rsi,1)>65))
            }
            stats['symbols']+=1;stats['candles']+=len(ts)
            for fam,mask in families.items():
                idx=np.flatnonzero(admissible&mask)
                if len(idx)==0:continue
                for side in (1,-1):
                    ix=idx[dirn[idx]==side]
                    if len(ix)==0:continue
                    pnl,ending=outcome_batch(ts,o,h,l,c,ix,side)
                    d={
                        'entry_ms':ts[ix+1],'symbol':doc['instId'],
                        'family':fam,'side':side,'body':bodies[ix],
                        'volratio':vratio[ix],'adx':adx[ix],
                    }
                    for k in range(len(EXITS)):
                        d[f'p{k}']=pnl[:,k]
                        d[f'end{k}']=ending[:,k]
                    frames.append(pd.DataFrame(d))
                    stats['signals']+=len(ix)
            count+=1
            if count%25==0:print('PROGRESS_CANDLES',count,'/',len(files),'signals',stats['signals'],flush=True)
        except Exception as exc:
            print('SKIP_CORRUPTED',p.name,type(exc).__name__,str(exc)[:110],flush=True)
            stats['invalid_candles']+=1
    if not frames:raise RuntimeError("No usable historical candle signals")
    ev=pd.concat(frames,ignore_index=True)
    # causal ordering and priority of strongest volume ratio for simultaneous alerts
    ev.sort_values(['entry_ms','volratio','symbol','family'],
                   ascending=[True,False,True,True],kind='mergesort',inplace=True)
    ev.reset_index(drop=True,inplace=True)
    tmin=int(ev.entry_ms.min());tmax=int(ev.entry_ms.max())
    assert tmin<tmax
    return ev,stats

def trade_sim(event_times,exit_times,net_rates,start_ms,end_ms):
    """Serial 1-position bankroll, no overlapping order, initial 20 USDT every segment."""
    ix=np.searchsorted(event_times,start_ms,side='left')
    endix=np.searchsorted(event_times,end_ms,side='left')
    equity=START_BALANCE
    busy=-1
    trades=0;wins=0;losses=0
    fees_and_slip=0.
    daily=defaultdict(lambda:[0,0,0])
    while ix<endix:
        t=int(event_times[ix])
        if t<busy:
            ix+=1
            continue
        fill=min(MAX_NOTIONAL,max(0.,equity-CASH_RESERVE))
        if fill<=0:
            break
        rate=float(net_rates[ix])
        amount=fill*rate
        equity+=amount
        fees_and_slip+=fill*TOTAL_COST
        trades+=1; wins+=amount>0;losses+=amount<0
        day=datetime.fromtimestamp(t/1000,timezone.utc).strftime('%Y-%m-%d')
        daily[day][0]+=amount;daily[day][1]+=1;daily[day][2]+=int(amount>0)
        busy=int(exit_times[ix])
        ix=np.searchsorted(event_times,busy,side='left')
    return {'entries':trades,'wins':wins,'losses':losses,'win_rate_pct':round(100*wins/trades,3) if trades else None,
            'start_usdt':20.,'end_usdt':round(equity,4),
            'net_usdt':round(equity-20.,4),'net_roi_pct':round(100*(equity/20-1),4),
            'fees_slippage_usdt':round(fees_and_slip,4),
            'active_days':len(daily),'positive_days':sum(x[0]>0 for x in daily.values()),
            'negative_days':sum(x[0]<0 for x in daily.values()),
            'daily':{d:{'net_usdt':round(v[0],4),'entries':v[1],'wins':v[2]} for d,v in sorted(daily.items())}}

def score(ev,params,start_ms,end_ms):
    """Cache candidate eligibility once for all TP/SL exits; minimize full-table scans."""
    fam,direction,body,vol,adx,k=params
    if not hasattr(score,'cache'):
        score.cache={}
        score.arrays={'family':ev['family'].to_numpy(),
                      'side':ev['side'].to_numpy(),
                      'body':ev['body'].to_numpy(),
                      'volratio':ev['volratio'].to_numpy(),
                      'adx':ev['adx'].to_numpy(),
                      'entry':ev['entry_ms'].to_numpy()}
        for z in range(len(EXITS)):
            score.arrays['p'+str(z)]=ev['p'+str(z)].to_numpy()
            score.arrays['end'+str(z)]=ev['end'+str(z)].to_numpy()
    a=score.arrays
    key=(fam,direction,body,vol,adx)
    if key not in score.cache:
        mask=(a['family']==fam)&(a['body']>=body)&(a['volratio']>=vol)&(a['adx']>=adx)
        if direction=='LONG':mask &= a['side']==1
        if direction=='SHORT':mask &= a['side']==-1
        score.cache[key]=np.flatnonzero(mask)
    ix=score.cache[key]
    return trade_sim(a['entry'][ix],a['end'+str(k)][ix],a['p'+str(k)][ix],start_ms,end_ms)

def brief(s):
    return {k:v for k,v in s.items() if k!='daily'}

def utcday(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).date().isoformat()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--archive',default='existing-90d/candles')
    ap.add_argument('--universe',default='research/blofin_free_history/latest_summary.json')
    ap.add_argument('--out',default='full90-report')
    args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    summary=json.loads(Path(args.universe).read_text())
    ev,coverage=build_events(args.archive,summary)
    expected_start=int(datetime.fromisoformat(summary['period_start_utc']).timestamp()*1000)
    expected_end=int(datetime.fromisoformat(summary['period_end_utc']).timestamp()*1000)
    length_days=(expected_end-expected_start)/86400000
    if length_days<89 or coverage['symbols']<190:
        raise RuntimeError('Refuse incomplete 90d dataset; got '+str(coverage))
    # Cover ALL 90d, with a 5m warm-up. First timestamp is 90d archive start.
    train_end=expected_start+60*86400000
    valid_end=expected_start+75*86400000
    test_end=expected_end
    candidates=[]
    start=time.time()
    for family in FAMILY:
        for side in SIDES:
            for b in BODIES:
                for vol in VOLUMES:
                    for adx in ADXS:
                        for k in range(len(EXITS)):
                            candidate=(family,side,b,vol,adx,k)
                            x=score(ev,candidate,expected_start,train_end)
                            if x['entries']<MIN_ENTRIES_TRAIN or x['active_days']<MIN_ACTIVE_DAYS_TRAIN or x['net_usdt']<=0:
                                continue
                            candidates.append((candidate,brief(x)))
    # Holdout remains untouched: train selects all train-positive, validation chooses a single best.
    candidates.sort(key=lambda t:(t[1]['net_usdt'],t[1]['positive_days'],t[1]['entries']),reverse=True)
    contenders=[]
    for candidate,tr in candidates:
        val=score(ev,candidate,train_end,valid_end)
        contenders.append({'rule':candidate,'train':tr,'validation':brief(val),
                           'validation_pass':val['entries']>=MIN_ENTRIES_VALID and val['net_usdt']>0})
    acceptable=[x for x in contenders if x['validation_pass']]
    acceptable.sort(key=lambda z:(z['validation']['net_usdt'],z['validation']['positive_days'],z['train']['net_usdt']),reverse=True)
    winner=acceptable[0] if acceptable else None
    if winner is not None:
        test=score(ev,tuple(winner['rule']),valid_end,test_end)
        training=score(ev,tuple(winner['rule']),expected_start,train_end)
        validation=score(ev,tuple(winner['rule']),train_end,valid_end)
        final={'rules':list(winner['rule']),
               'exit_tp_sl_hold':EXITS[int(winner['rule'][-1])],
               'train':training,'validation':validation,'holdout':test,
               'holdout_profitable_after_costs':test['net_usdt']>0 and test['entries']>=MIN_ENTRIES_VALID}
        verdict=('HISTORICAL_OUT_OF_SAMPLE_PROFIT_NOT_GUARANTEED'
                 if final['holdout_profitable_after_costs'] else
                 'NO_CONFIRMED_PROFIT_ON_UNSEEN_LAST_15_DAYS')
    else:
        final=None
        verdict='NO_FILTER_PASSED_TRAIN_AND_VALIDATION'
    baseline=('FLIP','BOTH',0.45,1.75,20,0)
    reference={k:score(ev,baseline,*bounds) for k,bounds in (
        ('train',(expected_start,train_end)),('validation',(train_end,valid_end)),
        ('holdout',(valid_end,test_end)))}
    report={
       'status':'COMPLETE','verdict':verdict,
       'data_days':length_days,'universe_symbols':coverage['symbols'],
       'coverage':coverage,'first_archive_date_utc':summary['period_start_utc'],
       'last_archive_date_utc':summary['period_end_utc'],
       'split':{'train_days':60,'validation_days':15,'holdout_days':15,
          'train_end_utc':utc(train_end),'validation_end_utc':utc(valid_end)},
       'strategies_considered':len(FAMILY)*len(SIDES)*len(BODIES)*len(VOLUMES)*len(ADXS)*len(EXITS),
       'families':FAMILY,'fees_roundtrip_pct':100*FEE_PCT,
       'assumed_adverse_slippage_pct':100*SLIPPAGE_PCT,'total_cost_pct':100*TOTAL_COST,
       'portfolio':'20 USDT global, 1x, single position, max 19.50 notional, reserve 0.50; no leverage',
       'entry':'next 5m candle open after CLOSED signal; strongest vol/MA20 wins same timestamp',
       'exits':'fixed TP/SL or max hold; SL always first on ambiguous bar; no futures price knowledge at entry',
       'universe_warning':'200 survivors selected on 2026-10-23, not historical TOP200; no delisted coins.',
       'selection_warning':'Optimize first 60d, validate next 15d, score single FROZEN candidate on final 15d. No retuning based on holdout.',
       'model_warning':'This tests listed OHLCV strategies, NOT exact live tunnel/broker fills. No tick/contract minimums, changing spreads, funding, outages, funding, exchange risk or unrealizable fast fills.',
       'training_positive_rules':len(candidates),'validation_positive_in_train_survivors':len(acceptable),
       'top_train_candidates':contenders[:10],
       'chosen':final,'reference_volume_flip':reference,
       'elapsed_s':round(time.time()-start,1)}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    daily_rows=[]
    if final:
        for part in ('train','validation','holdout'):
            for day,item in final[part]['daily'].items():
                daily_rows.append({'period':part,'day_utc':day,**item})
    import csv
    with (out/'daily.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['period','day_utc','net_usdt','entries','wins'])
        w.writeheader();w.writerows(daily_rows)
    md=['# Pełny test 90 dni BloFin, 200 coinów — READ ONLY','',
        '**Werdykt: '+verdict+'**','',
        'Świece: '+str(coverage['candles'])+'; instrumenty: '+str(coverage['symbols'])+
        '; sygnały: '+str(coverage['signals'])+'.',
        'Przetestowane ustawienia: '+str(report['strategies_considered'])+'.',
        'Wynik: '+str(len(candidates))+' ustawień dodatnich na treningu, '+str(len(acceptable))+' na walidacji (wśród 50 najlepszych treningowo).','',
        '## Domyślny FLIP 90 dni — uproszczona wersja wejścia',
        str({k:brief(v) for k,v in reference.items()}),'',
        '## Wynik jednej wybranej strategii',
        str({k:brief(v) if isinstance(v,dict) and 'entries' in v else v for k,v in final.items()}) if final else 'Żaden wariant nie przeszedł obu pierwszych okresów.','',
        '## Ograniczenia',report['universe_warning'],report['selection_warning'],report['model_warning'],
        'Nie włączono żadnego filtra do LIVE. Wyniki historyczne nie gwarantują przyszłych zysków.']
    (out/'README.md').write_text('\n'.join(md)+'\n')
    print('FULL90_RESULT='+json.dumps({'status':report['status'],'verdict':verdict,
           'symbols':coverage['symbols'],'candles':coverage['candles'],'signals':coverage['signals'],
           'strategies':report['strategies_considered'],'training_positive':len(candidates),
           'validation_positive':len(acceptable),
           'chosen_rule':final['rules'] if final else None,
           'chosen_train':brief(final['train']) if final else None,
           'chosen_valid':brief(final['validation']) if final else None,
           'chosen_holdout':brief(final['holdout']) if final else None,
           'baseline_holdout':brief(reference['holdout'])},separators=(',',':')),flush=True)

if __name__=='__main__':
    main()
