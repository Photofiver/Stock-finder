#!/usr/bin/env python3
"""Compare body strength, ADX and both on real closed BloFin trades (READ ONLY).
Input = pre-entry snapshot + already saved 90d 5m candles.
Labels = verified realized NET PnL. Never fetches exchange data / places orders.
"""
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from blofin_hourly_filter_lab import load as load_verified_history
from research.blofin_ml_winloss import add_archive_features

BODY_CUTOFFS = (0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
ADX_CUTOFFS = (18, 20, 22, 24, 26, 28, 30)
EMBARGO_MS = 30 * 60 * 1000

def meets(trade, body=None, adx=None):
    f=trade['features']
    return ((body is None or f['bar_body_pct'] >= body) and
            (adx is None or f['bar_adx14'] >= adx))

def score(rows, accepted):
    subset=[t for t,yes in zip(rows,accepted) if yes]
    rejected=[t for t,yes in zip(rows,accepted) if not yes]
    n=len(rows)
    wins=sum(t['net']>0 for t in subset)
    losses=sum(t['net']<0 for t in subset)
    fees=sum(t['fee'] for t in subset)
    original=sum(t['net'] for t in rows)
    new=sum(t['net'] for t in subset)
    return {
        'total_candidates':n,'accepted':len(subset),
        'accepted_pct':round(len(subset)/n*100,2) if n else None,
        'wins':wins,'losses':losses,'flat':len(subset)-wins-losses,
        'win_rate_pct':round(wins/len(subset)*100,2) if subset else None,
        'blocked_wins':sum(t['net']>0 for t in rejected),
        'blocked_losses':sum(t['net']<0 for t in rejected),
        'blocked_flats':sum(t['net']==0 for t in rejected),
        'net_usdt_after_fees':round(new,7),
        'commissions_usdt':round(fees,7),
        'baseline_net_usdt_after_fees':round(original,7),
        'difference_vs_unfiltered_usdt':round(new-original,7),
        'mean_net_return_on_notional_pct':round(
            100*sum(t['rate'] for t in subset)/len(subset),5) if subset else None,
        'sum_net_returns_pct_points_NOT_ROI':round(
            100*sum(t['rate'] for t in subset),5),
    }

def compare(rows, body=None, adx=None):
    return score(rows,[meets(t,body,adx) for t in rows])

def fmt(v):
    return 'brak' if v is None else str(v)

def stamp(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--state',default='blofin_live_state.json')
    ap.add_argument('--candles',default='existing-90d/candles')
    ap.add_argument('--out',default='adx-body-report')
    args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    all_trades, excluded=load_verified_history(args.state)
    archive_info=add_archive_features(all_trades,args.candles)
    rows=[t for t in all_trades if
          'bar_adx14' in t['features'] and 'bar_body_pct' in t['features']]
    rows.sort(key=lambda t:(t['opened_ms'],t['id']))
    if len(rows)<100:
        raise RuntimeError('Too few matched real trades (>=100 required): '+str(len(rows)))
    a=int(len(rows)*.6);b=int(len(rows)*.8)
    val_begin=rows[a]['opened_ms']
    test_begin=rows[b]['opened_ms']
    train=[t for t in rows[:a] if t['closed_ms']<=val_begin-EMBARGO_MS]
    valid=[t for t in rows[a:b] if t['closed_ms']<=test_begin-EMBARGO_MS]
    test=rows[b:]
    if min(len(train),len(valid),len(test))<20:
        raise RuntimeError('Too few observations in chronological split')
    candidates=([('KORPUS',bd,None) for bd in BODY_CUTOFFS]
              +[('ADX',None,ad) for ad in ADX_CUTOFFS]
              +[('KORPUS+ADX',bd,ad) for bd in BODY_CUTOFFS for ad in ADX_CUTOFFS])
    diagnostics=[]
    for kind,body,adx in candidates:
        s_train=compare(train,body,adx)
        s_valid=compare(valid,body,adx)
        diagnostics.append({
            'kind':kind,'body_min_pct':body,'adx_min':adx,
            'train_accepted':s_train['accepted'],
            'train_wins':s_train['wins'],'train_losses':s_train['losses'],
            'train_net_usdt':s_train['net_usdt_after_fees'],
            'validation_accepted':s_valid['accepted'],
            'validation_wins':s_valid['wins'],
            'validation_losses':s_valid['losses'],
            'validation_net_usdt':s_valid['net_usdt_after_fees'],
            'train_pass':bool(s_train['accepted']>=15 and s_train['net_usdt_after_fees']>0),
            'validation_pass':bool(s_valid['accepted']>=8 and s_valid['net_usdt_after_fees']>0)
        })
    acceptable=[d for d in diagnostics if d['train_pass'] and d['validation_pass']]
    acceptable.sort(key=lambda d:(-d['validation_net_usdt'], -d['validation_accepted'],
                                  -d['train_net_usdt'],d['kind']))
    winner=acceptable[0] if acceptable else None
    # Only one selected rule, frozen BEFORE any holdout metrics were read.
    frozen_prespecified=[
      ('BEZ FILTRA',None,None),
      ('KORPUS >= 0.25%',.25,None),
      ('ADX >= 22',None,22),
      ('KORPUS >= 0.25% i ADX >= 22',.25,22),
    ]
    if winner is not None:
        frozen_prespecified.append(('WYBRANE NA TRENINGU+WALIDACJI',
                                     winner['body_min_pct'],winner['adx_min']))
    report_rows=[]
    for name,body,adx in frozen_prespecified:
        r={'name':name,'body_min_pct':body,'adx_min':adx,
           'train':compare(train,body,adx),
           'validation':compare(valid,body,adx),
           'holdout':compare(test,body,adx)}
        report_rows.append(r)
    baseline=report_rows[0]['holdout']
    best=report_rows[-1]['holdout'] if winner else None
    verdict='BRAK FILTRA Z POTWIERDZONYM DODATNIM WYNIKIEM NA TRENINGU I WALIDACJI'
    if winner:
        if best['accepted']>=8 and best['net_usdt_after_fees']>0 and best['net_usdt_after_fees']>baseline['net_usdt_after_fees']:
            verdict='SYGNAL NA HOLDOUCIE - wymaga kolejnego niezaleznego okresu'
        else:
            verdict='WYBRANY FILTR NIE POTWIERDZONY NA HOLDOUCIE'
    result={
        'status':'DONE','verdict':verdict,'filter_selection_before_holdout':True,
        'total_verified_closed':len(all_trades),
        'both_indicators_available':len(rows),
        'missing_either_indicator':len(all_trades)-len(rows),
        'archive_info':archive_info,
        'rejected_history':excluded,
        'train_period':[stamp(train[0]['opened_ms']),stamp(train[-1]['opened_ms'])],
        'validation_period':[stamp(valid[0]['opened_ms']),stamp(valid[-1]['opened_ms'])],
        'holdout_period':[stamp(test[0]['opened_ms']),stamp(test[-1]['opened_ms'])],
        'split_counts':{'train':len(train),'validation':len(valid),'holdout':len(test)},
        'tested_cutoffs':{'body_pct':BODY_CUTOFFS,'adx':ADX_CUTOFFS},
        'number_of_rules_considered':len(diagnostics),
        'winner_rule_frozen_before_test':winner,
        'comparisons':report_rows,
        'notes':[
          'Real trade PnL amounts are on varying historical position notionals; these are NOT 20 USDT portfolio returns.',
          'Profit/loss labels include recorded exchange fees.',
          'Historical 90d TOP200 is current-snapshot survivor selection, not the past full universe.',
          'Bar features are only from CLOSED candles preceding actual entry.',
          'Only originally taken LIVE trades analyzed, not skipped opportunities.',
          'Multiple threshold exploration can overfit even validation; holdout verdict is not proof of future profitability.',
          'No modification of LIVE strategy, orders, TP, SL, risk, or account.'
        ]
    }
    flat=[]
    for x in diagnostics:
        flat.append({k:(int(v) if isinstance(v,bool) else v) for k,v in x.items()})
    with (out/'training_validation_grid.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(flat[0]));w.writeheader();w.writerows(flat)
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines=['# Test korpusu świecy i ADX — BloFin LIVE (TYLKO ODCZYT)','',
        '**Werdykt:** '+verdict,'',
        'Weryfikowane zamknięte transakcje: '+str(len(all_trades))+'.',
        'Cechy ADX i korpusu z zamkniętych świec dostępne dla: '+str(len(rows))+'.',
        'Chronologicznie: trening '+str(len(train))+', walidacja '+str(len(valid))+', późniejszy test '+str(len(test))+'.',
        'Test: '+stamp(test[0]['opened_ms'])+' — '+stamp(test[-1]['opened_ms'])+'.','',
        '| Reguła | Przyjęte | Wygrane | Straty | Odrzucone straty | Odrzucone zyski | Netto USDT | Zmiana netto vs bez filtra |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for x in report_rows:
        t=x['holdout']
        lines.append('| '+x['name']+' | '+str(t['accepted'])+
          ' | '+str(t['wins'])+' | '+str(t['losses'])+
          ' | '+str(t['blocked_losses'])+' | '+str(t['blocked_wins'])+
          ' | '+str(t['net_usdt_after_fees'])+
          ' | '+str(t['difference_vs_unfiltered_usdt'])+' |')
    lines.extend(['','## Zamrożony filtr z treningu + walidacji',
        'Brak spełniającej oba okresy reguły.' if winner is None else json.dumps(winner,ensure_ascii=False),
        '','## Ograniczenia'])
    lines += ['- '+v for v in result['notes']]
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('ADX_BODY_RESULT='+json.dumps({k:v for k,v in result.items() if k not in ('rejected_history','notes')},ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__':
    main()
