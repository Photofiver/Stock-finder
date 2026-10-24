#!/usr/bin/env python3
"""READ ONLY: explain recorded LIVE wins/losses using XGBoost, TreeSHAP and 90d candles.
Never calls BloFin, accesses API credentials, modifies trading files, or places orders.
"""
import argparse
import bisect
import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys

# Scripts are launched as research/foo.py; repository-root module must be importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from blofin_hourly_filter_lab import load as load_verified_history

FIVE = 300000

def candle_features(bars, opened_ms):
    """An archive candle timestamp is its OPEN. Only ts+5m <= actual trade open."""
    if len(bars) < 120:
        return {}
    ts = [int(x[0]) for x in bars]
    end = bisect.bisect_right(ts, opened_ms - FIVE)
    if end < 120:
        return {}
    rows = bars[max(0, end-170):end]
    stamp = [int(x[0]) for x in rows]
    if opened_ms - (stamp[-1] + FIVE) > 20*60000:
        return {}
    if any(b-a != FIVE for a,b in zip(stamp[-66:-1],stamp[-65:])):
        return {}
    try:
        op = pd.Series([float(x[1]) for x in rows])
        hi = pd.Series([float(x[2]) for x in rows])
        lo = pd.Series([float(x[3]) for x in rows])
        cl = pd.Series([float(x[4]) for x in rows])
        vol = pd.Series([float(x[5]) for x in rows])
        if (cl <= 0).any() or (vol < 0).any():
            return {}
        prev = cl.shift(1)
        change = cl.diff()
        up = change.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
        dn = (-change).clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
        rsi = 100 - 100/(1 + up/dn.replace(0,np.nan))
        if float(dn.iloc[-1]) == 0 and float(up.iloc[-1]) > 0:
            rsi_v = 100.
        else:
            rsi_v = float(rsi.iloc[-1])
        ema12=cl.ewm(span=12,adjust=False).mean()
        ema26=cl.ewm(span=26,adjust=False).mean()
        macd=ema12-ema26
        hist=macd-macd.ewm(span=9,adjust=False).mean()
        plus=hi.diff(); minus=-lo.diff()
        pdm=plus.where((plus>minus)&(plus>0),0).ewm(alpha=1/14,adjust=False).mean()
        mdm=minus.where((minus>plus)&(minus>0),0).ewm(alpha=1/14,adjust=False).mean()
        tr=pd.concat((hi-lo,(hi-prev).abs(),(lo-prev).abs()),axis=1).max(axis=1)
        atr=tr.ewm(alpha=1/14,adjust=False).mean()
        pdi=100*pdm/atr.replace(0,np.nan)
        mdi=100*mdm/atr.replace(0,np.nan)
        dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
        adx=dx.ewm(alpha=1/14,adjust=False).mean()
        stoch=100*(cl-lo.rolling(14).min())/(hi.rolling(14).max()-lo.rolling(14).min()).replace(0,np.nan)
        p=float(cl.iloc[-1]); o=float(op.iloc[-1]); v=float(vol.iloc[-1])
        avg=float(vol.iloc[-21:-1].mean())
        color = 1 if p>o else -1 if p<o else 0
        last_opposite = None
        run=1
        for i in range(len(rows)-2,max(-1,len(rows)-12),-1):
            c=1 if cl.iloc[i]>op.iloc[i] else -1 if cl.iloc[i]<op.iloc[i] else 0
            if c==color:
                if last_opposite is None: run+=1
            else:
                if last_opposite is None and c!=0: last_opposite=float(vol.iloc[i])
                break
        def clean(obj):
            return {k:float(x) for k,x in obj.items()
                    if x is not None and math.isfinite(float(x))}
        return clean({
            'bar_rsi14':rsi_v, 'bar_adx14':adx.iloc[-1],
            'bar_plus_di14':pdi.iloc[-1], 'bar_minus_di14':mdi.iloc[-1],
            'bar_macd_hist_pct':100*hist.iloc[-1]/p,
            'bar_macd_change_pct':100*(hist.iloc[-1]-hist.iloc[-2])/p,
            'bar_stoch_k14':stoch.iloc[-1],
            'bar_stoch_d14':stoch.iloc[-3:].mean(),
            'bar_vol_ma20_ratio':v/avg if avg>0 else None,
            'bar_vol_last_opposite_ratio':v/last_opposite if last_opposite and last_opposite>0 else None,
            'bar_color':color, 'bar_same_color_run':run,
            'bar_body_pct':100*abs(p-o)/o,
            'bar_range_pct':100*(hi.iloc[-1]-lo.iloc[-1])/o,
            'bar_close_sma10_pct':100*(p/cl.iloc[-10:].mean()-1),
            'bar_close_sma20_pct':100*(p/cl.iloc[-20:].mean()-1),
            'bar_return_15m_pct':100*(p/cl.iloc[-4]-1),
            'bar_return_60m_pct':100*(p/cl.iloc[-13]-1),
            'bar_atr14_pct':100*atr.iloc[-1]/p
        })
    except (TypeError,ValueError,ZeroDivisionError,OverflowError):
        return {}

def add_archive_features(trades, directory):
    paths={p.name.removesuffix('.json.gz'):p for p in Path(directory).rglob('*.json.gz')}
    grouped=defaultdict(list)
    for trade in trades:
        grouped[str(trade['coin'])].append(trade)
    counts=Counter()
    for symbol, subset in grouped.items():
        path=paths.get(symbol)
        if path is None:
            counts['symbols_missing']+=1
            continue
        try:
            with gzip.open(path,'rt',encoding='utf-8') as fh:
                doc=json.load(fh)
            if doc.get('instId')!=symbol or doc.get('bar')!='5m':
                counts['invalid_archive']+=1
                continue
            bars=doc.get('rows') or []
            for trade in subset:
                found=candle_features(bars,int(trade['opened_ms']))
                if found:
                    trade['features'].update(found)
                    counts['trades_with_archive_features']+=1
            counts['symbols_used']+=1
        except (OSError,ValueError,KeyError,json.JSONDecodeError):
            counts['invalid_archive']+=1
    counts['files_found']=len(paths)
    return dict(counts)

def review_diagnostics(trades, directory):
    identities={(t['coin'],int(t['opened_ms'])):t for t in trades}
    peaks=0; net_losses_with_peaks=0; reviewed=0
    seen=set()
    for path in sorted(Path(directory).glob('????-??-??.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                r=json.loads(line)
                key=(r['instrument'],int(r['opened_ms']))
                t=identities.get(key)
                if t is None or key in seen:
                    continue
                seen.add(key); reviewed+=1
                if float(r.get('best_observed_gross_pct_approx') or 0)>0:
                    peaks+=1
                    if float(t['net'])<0: net_losses_with_peaks+=1
            except (ValueError,KeyError,TypeError,json.JSONDecodeError):
                continue
    return {'matched_reviewed_trades':reviewed,
            'reviewed_with_observed_positive_excursion':peaks,
            'losing_reviews_that_were_earlier_positive':net_losses_with_peaks}

def stats(trades, indices):
    chosen=[trades[i] for i in indices]
    wins=sum(t['net']>0 for t in chosen)
    return {'positions':len(chosen), 'wins':wins,'losses':sum(t['net']<0 for t in chosen),
            'win_rate_pct':round(wins*100/len(chosen),2) if chosen else None,
            'actual_net_usdt':round(sum(t['net'] for t in chosen),7),
            'actual_fees_usdt':round(sum(t['fee'] for t in chosen),7)}

def fixed20_one_position_scenario(trades, indices):
    """Illustration only; does not simulate BloFin size limits, spread or fills."""
    bankroll=20.; busy=0; count=0; skipped=0
    for idx in indices:
        t=trades[idx]
        if t['opened_ms'] < busy:
            skipped+=1
            continue
        if bankroll<=0:break
        bankroll+=min(20.,bankroll)*float(t['rate'])
        busy=t['closed_ms']; count+=1
    return {'scenario_20usdt_1x_roi_pct':round((bankroll/20-1)*100,4),
            'executed_nonoverlapping':count,'skipped_overlapping':skipped,
            'warning':'Hypothetical normalized net returns: not exchange-executable sizing or real portfolio ROI.'}

def write_csv(path, rows, columns):
    with path.open('w',newline='',encoding='utf-8') as fh:
        w=csv.DictWriter(fh,fieldnames=columns,extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

def run(args):
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    trades, rejected = load_verified_history(args.state)
    if not trades:
        raise RuntimeError('No verified closed trades with pre-entry snapshots; no model trained')
    # Pre-entry snapshot safety and forbidden ex-post fields are enforced by existing lab.load.
    candles=add_archive_features(trades,args.candles)
    reviews=review_diagnostics(trades,args.reviews)
    trades.sort(key=lambda t:(t['opened_ms'],t['id']))
    summary={
        'READ_ONLY':True,'actual_closed_trades_with_valid_snapshots':len(trades),
        'discarded_records':rejected,'candle_coverage':candles,
        'review_diagnostics':reviews,
        'total_win_loss':stats(trades,list(range(len(trades)))),
        'status':'NO_MODEL',
        'notes':['Only actual verified executed LIVE trade results, not 90 days of labeled trades.',
                 '90d archive used for entry-time OHLCV indicators, NOT hypothetical fills.',
                 'No knowledge after trade entry is used as an ML feature.',
                 'The 200-symbol historical universe was selected at the end of its 90d period.',
                 'Only trades the old bot entered are labeled; rejected signals not analyzed.',
                 'Saved post-entry TP/SL and observed price peak are excluded as model inputs.']
    }
    md=['# Analiza BloFin: XGBoost + SHAP (tylko odczyt)','',
        'LIVE, zlecenia i ustawienia bota pozostają bez zmian.','',
        'Potwierdzone transakcje z cechami chwili wejścia: '+str(len(trades))+'.',
        'Zyski/straty po prowizjach: '+str(summary['total_win_loss'])+'.',
        'Obserwowane wstępne zyski stratnych transakcji: '+str(reviews)+'.','']
    if len(trades)<90:
        summary['status']='TOO_FEW_LABELED_TRADES'
    else:
        from xgboost import XGBClassifier
        from sklearn.metrics import roc_auc_score
        n=len(trades)
        a,b=int(.6*n),int(.8*n)
        # Chronology + 30min embargo; no overlapping positions across train/validation.
        train=[i for i in range(a) if trades[i]['closed_ms']<=trades[a]['opened_ms']-1800000]
        valid=[i for i in range(a,b) if trades[i]['closed_ms']<=trades[b]['opened_ms']-1800000]
        test=list(range(b,n))
        y=np.asarray([1 if t['net']>0 else 0 for t in trades])
        cols=sorted({str(k) for t in trades for k,v in t['features'].items()
                     if isinstance(v,(int,float,bool)) and math.isfinite(float(v))
                     and not any(ch in str(k) for ch in '[]<')})
        X=pd.DataFrame([{k:float(v) for k,v in t['features'].items()
                         if k in cols and isinstance(v,(int,float,bool)) and math.isfinite(float(v))}
                        for t in trades],columns=cols,dtype=float)
        cols=[k for k in cols if X.iloc[train][k].notna().sum()>=max(15,int(len(train)*.12))
              and X.iloc[train][k].nunique(dropna=True)>1]
        X=X[cols]
        if (min(len(train),len(valid),len(test))<15 or len(cols)<2
                or min(sum(y[train]),len(train)-sum(y[train]))<8
                or min(sum(y[valid]),len(valid)-sum(y[valid]))<3):
            summary['status']='INSUFFICIENT_CHRONOLOGICAL_TRAIN_VALIDATION'
        else:
            model=XGBClassifier(n_estimators=180,max_depth=3,learning_rate=.035,
                subsample=.85,colsample_bytree=.85,min_child_weight=6,
                reg_lambda=12,objective='binary:logistic',
                tree_method='hist',random_state=42,n_jobs=2)
            model.fit(X.iloc[train],y[train])
            pval=model.predict_proba(X.iloc[valid])[:,1]
            ptest=model.predict_proba(X.iloc[test])[:,1]
            # Select threshold ONLY on validation, require positive net and >=5 trades.
            options=[]
            for threshold in (.5,.55,.6,.65,.7,.75,.8,.85,.9):
                selected=[i for i,p in zip(valid,pval) if p>=threshold]
                net=sum(trades[i]['net'] for i in selected)
                if len(selected)>=5 and net>0:
                    options.append((net,len(selected),threshold))
            threshold=sorted(options,key=lambda z:(-z[0],-z[1],z[2]))[0][2] if options else None
            accepted=[i for i,p in zip(test,ptest) if threshold is not None and p>=threshold]
            accepted_validation=[i for i,p in zip(valid,pval) if threshold is not None and p>=threshold]
            summary['status']='TRAINED'
            summary['model']={
                'train':stats(trades,train),'validation_all':stats(trades,valid),
                'validation_filter':stats(trades,accepted_validation),
                'test_all':stats(trades,test),'test_filter':stats(trades,accepted),
                'validation_selected_threshold':threshold,
                'test_auc':round(float(roc_auc_score(y[test],ptest)),5) if len(set(y[test]))==2 else None,
                'test_start_utc':datetime.fromtimestamp(trades[b]['opened_ms']/1000,timezone.utc).isoformat(),
                'scenario_20usdt':fixed20_one_position_scenario(trades,accepted),
                'input_features':cols
            }
            output=[]
            for i,p in zip(test,ptest):
                t=trades[i]
                output.append({'entry_utc':datetime.fromtimestamp(t['opened_ms']/1000,timezone.utc).isoformat(),
                    'coin':t['coin'],'side':t['side'],'win_probability':round(float(p),6),
                    'accepted':int(i in accepted),'actual_net_usdt':t['net'],
                    'win':int(t['net']>0)})
            write_csv(out/'holdout_predictions.csv',output,list(output[0]))
            winners=[i for i in train if y[i]]
            losers=[i for i in train if not y[i]]
            medians=[]
            for key in cols:
                w=X.loc[winners,key].dropna(); l=X.loc[losers,key].dropna()
                if len(w)>=5 and len(l)>=5:
                    medians.append({'indicator':key,'winner_median':round(float(w.median()),6),
                       'loser_median':round(float(l.median()),6),
                       'winner_count':len(w),'loser_count':len(l)})
            write_csv(out/'median_winners_vs_losers.csv',medians,
                ['indicator','winner_median','loser_median','winner_count','loser_count'])
            try:
                import shap
                values=np.asarray(shap.TreeExplainer(model).shap_values(X.iloc[test][:300]))
                if values.ndim==3:values=values[:,:,1]
                importance=[{'indicator':c,'mean_absolute_shap':round(float(v),8)}
                            for c,v in zip(cols,np.abs(values).mean(axis=0))]
                importance.sort(key=lambda z:-z['mean_absolute_shap'])
                write_csv(out/'shap_importance.csv',importance,
                          ['indicator','mean_absolute_shap'])
                summary['shap']='SUCCESS'
            except Exception as exc:
                importance=[]
                summary['shap']='ERROR '+type(exc).__name__
            report=summary['model']
            md.extend(['## Wynik na późniejszych transakcjach (nienaruszony test)','',
                'Bez filtra: '+str(report['test_all']),
                'Z filtrem: '+str(report['test_filter']),
                'Testowe hipotetyczne 20 USDT: '+str(report['scenario_20usdt']),
                'Próg wybrany na walidacji: '+str(threshold),
                'AUC: '+str(report['test_auc']),'',
                '## Wskaźniki o największym wpływie (SHAP)',''])
            md.extend('- '+r['indicator']+': '+str(r['mean_absolute_shap']) for r in importance[:20])
    md.extend(['','## Ograniczenia']+['- '+s for s in summary['notes']])
    md.append('Nie włączać wybranych filtrów do LIVE bez niezależnej dalszej walidacji.')
    (out/'report.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('ML_REPORT='+json.dumps({k:v for k,v in summary.items() if k!='notes'},ensure_ascii=False),flush=True)

def self_test():
    from blofin_hourly_filter_lab import entry_ok, flatten
    opening=1700000000000
    assert not entry_ok({'signal_close_ms':opening+1,'rsi14':60},opening)
    assert entry_ok({'signal_close_ms':opening-1,'rsi14':60},opening)
    assert 'gross_pnl_usdt' not in flatten({'gross_pnl_usdt':20,'rsi14':44})
    start=1800000000000
    start-=start%FIVE
    bars=[[str(start+i*FIVE),'100','101','99',str(100+i*.001),'100','0','0','1'] for i in range(160)]
    opening=start+151*FIVE+1000
    baseline=candle_features(bars,opening)
    assert baseline and baseline['bar_vol_ma20_ratio']>0
    bars[151][5]='99999999'
    assert baseline==candle_features(bars,opening), 'Future candle leakage'
    print('SELF_TEST_CAUSALITY_OK')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--state',default='blofin_live_state.json')
    p.add_argument('--reviews',default='blofin_fast_scan_events/trade_reviews')
    p.add_argument('--candles',default='existing-90d/candles')
    p.add_argument('--out',default='ml-report')
    p.add_argument('--self-test',action='store_true')
    args=p.parse_args()
    self_test() if args.self_test else run(args)

if __name__=='__main__':
    main()
