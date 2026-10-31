#!/usr/bin/env python3
"""READ ONLY. Verify ALL 11 strategies qualified on prior 60d train + 15d validation.
Same original engine, candle archives, transaction costs, selection and period bounds.
No optimization on last 15d. Do not modify LIVE, no broker network calls.
"""
from __future__ import annotations
import argparse
import csv
import itertools
import json
from pathlib import Path
from datetime import datetime
import research.blofin_full90_strong_signals as m

def b(x):
    return {k:v for k,v in x.items() if k!='daily'}

def desc(r):
    fam,side,body,vol,adx,exitidx=r
    tp,sl,hold=m.EXITS[exitidx]
    return {
       'family':fam,'side':side,'body_min_pct':body,
       'volume_over_ma20':vol,'adx14_min':adx,
       'tp_pct':round(tp*100,4),'sl_pct':round(sl*100,4),
       'max_hold_min':hold*5
    }

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',default='existing-90d/candles')
    parser.add_argument('--universe',default='research/blofin_free_history/latest_summary.json')
    parser.add_argument('--out',default='verify-11-report')
    a=parser.parse_args()
    out=Path(a.out)
    out.mkdir(parents=True,exist_ok=True)
    metadata=json.loads(Path(a.universe).read_text(encoding='utf-8'))
    ev,coverage=m.build_events(a.archive,metadata)
    start=int(datetime.fromisoformat(metadata['period_start_utc']).timestamp()*1000)
    end=int(datetime.fromisoformat(metadata['period_end_utc']).timestamp()*1000)
    assert (end-start)/86400000>=89 and coverage['symbols']>=190
    day60=start+60*86400000
    day75=start+75*86400000
    training_positive=[]
    qualified=[]
    for rule in itertools.product(m.FAMILY,m.SIDES,m.BODIES,m.VOLUMES,m.ADXS,range(len(m.EXITS))):
        tr=m.score(ev,rule,start,day60)
        if tr['entries']<m.MIN_ENTRIES_TRAIN or tr['active_days']<m.MIN_ACTIVE_DAYS_TRAIN or tr['net_usdt']<=0:
            continue
        training_positive.append(rule)
        va=m.score(ev,rule,day60,day75)
        if va['entries']<m.MIN_ENTRIES_VALID or va['net_usdt']<=0:
            continue
        qualified.append((rule,tr,va))
    qualified.sort(key=lambda x:(x[2]['net_usdt'],x[2]['positive_days'],x[1]['net_usdt']),reverse=True)
    entries=[]
    for rank,(rule,tr,va) in enumerate(qualified,1):
        ho=m.score(ev,rule,day75,end)
        entries.append({
           'rank_by_validation_only':rank,
           'rule':desc(rule),
           'train':tr,'validation':va,'holdout':ho,
           'holdout_net_positive':ho['net_usdt']>0,
           'holdout_positive_and_min_10_entries':ho['net_usdt']>0 and ho['entries']>=m.MIN_ENTRIES_VALID,
           'all_3_segments_positive':tr['net_usdt']>0 and va['net_usdt']>0 and ho['net_usdt']>0
        })
    positive=sum(x['holdout_net_positive'] for x in entries)
    positive_volume=sum(x['holdout_positive_and_min_10_entries'] for x in entries)
    summary={'status':'COMPLETE','comparison_count':len(entries),
             'training_profitable_total':len(training_positive),
             'positive_on_holdout':positive,
             'positive_with_min10_holdout_entries':positive_volume,
             'negative_on_holdout':sum(x['holdout']['net_usdt']<0 for x in entries),
             'holdout_zero_net':sum(x['holdout']['net_usdt']==0 for x in entries),
             'periods':{'train':[m.utc(start),m.utc(day60)],
                        'validation':[m.utc(day60),m.utc(day75)],
                        'holdout':[m.utc(day75),m.utc(end)]},
             'source':{'symbols':coverage['symbols'],
                       'candles':coverage['candles'],
                       'signals':coverage['signals'],
                       'tested_rules':len(m.FAMILY)*len(m.SIDES)*len(m.BODIES)*len(m.VOLUMES)*len(m.ADXS)*len(m.EXITS)},
             'settings':{'initial_capital_usdt':m.START_BALANCE,
                         'max_one_position_notional_usdt':m.MAX_NOTIONAL,
                         'roundtrip_assumed_fee_pct':m.FEE_PCT*100,
                         'roundtrip_assumed_slippage_pct':m.SLIPPAGE_PCT*100,
                         'sl_first_ambiguity':True},
             'verification_rule':'All strategies selected using first 60+15 days, holdout 15 days evaluated AFTER selection; no holdout tuning.',
             'caveats':['All strategies here use same source and potentially overlapping price signals: not 11 independent confirmations',
                        'Fixed as-of-last-day 200 symbol universe, so survivorship bias',
                        'No verified BloFin min contract, funding, spreads, depth, latency, or broker fills',
                        'One position globally and initial 20 USDT each independently reset segment',
                        'Data may contain 5m OHLC ambiguities; conservative SL-before-TP fill',
                        'Do not interpret historical net returns as future guarantees',
                        'Live trading not modified'],
             'strategies':entries}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    cols=['rank','family','side','body_pct_min','vol_ma20_min','adx14_min','tp_pct','sl_pct','max_hold_min',
          'train_trades','train_net_usdt','train_roi_pct','validation_trades','validation_net_usdt','validation_roi_pct',
          'holdout_trades','holdout_wins','holdout_losses','holdout_positive_days','holdout_negative_days',
          'holdout_net_usdt','holdout_roi_pct','holdout_positive_min_10']
    csv_rows=[]
    for x in entries:
        r=x['rule'];tr=x['train'];va=x['validation'];ho=x['holdout']
        csv_rows.append(dict(zip(cols,[
          x['rank_by_validation_only'],r['family'],r['side'],r['body_min_pct'],r['volume_over_ma20'],
          r['adx14_min'],r['tp_pct'],r['sl_pct'],r['max_hold_min'],
          tr['entries'],tr['net_usdt'],tr['net_roi_pct'],
          va['entries'],va['net_usdt'],va['net_roi_pct'],
          ho['entries'],ho['wins'],ho['losses'],ho['positive_days'],ho['negative_days'],
          ho['net_usdt'],ho['net_roi_pct'],x['holdout_positive_and_min_10_entries']])))
    with (out/'comparison.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(csv_rows)
    daily=[]
    for x in entries:
        for seg in ('train','validation','holdout'):
            for d,row in x[seg]['daily'].items():
                daily.append({'strategy_rank':x['rank_by_validation_only'],'period':seg,'date_utc':d,**row})
    with (out/'daily.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['strategy_rank','period','date_utc','net_usdt','entries','wins'])
        w.writeheader();w.writerows(daily)
    lines=['# Wszystkie 11 kwalifikowanych strategii — ostatnie 15 dni (READ ONLY)','',
           'Strategie wybrano BEZ znajomości wyników ostatnich 15 dni.','',
           f'Pozytywne po kosztach: {positive}/{len(entries)}; z >=10 wejściami: {positive_volume}/{len(entries)}.','',
           '| Rank (walidacja) | Strategia | Korpus % | Vol/MA20 | ADX | TP/SL/Max min | Wejścia | Wygrane | Stratne | Netto USDT | ROI % |',
           '|---:|---|---:|---:|---:|---|---:|---:|---:|---:|---:|']
    for x in entries:
        z=x['rule'];h=x['holdout']
        lines.append(f"| {x['rank_by_validation_only']} | {z['family']} {z['side']} | {z['body_min_pct']} | {z['volume_over_ma20']} | {z['adx14_min']} | {z['tp_pct']}/{z['sl_pct']}/{z['max_hold_min']} | {h['entries']} | {h['wins']} | {h['losses']} | {h['net_usdt']:+.4f} | {h['net_roi_pct']:+.4f} |")
    lines+=['','## Ważne zastrzeżenia']+['- '+z for z in summary['caveats']]
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('VERIFY_11_RESULT='+json.dumps({'status':'COMPLETE',
          'train_qualified':len(training_positive),'qualified':len(entries),
          'holdout_positive':positive,'holdout_positive_min10':positive_volume,
          'holdout_negative':summary['negative_on_holdout'],
          'holdout_zero':summary['holdout_zero_net'],
          'holdout':[{'rank':x['rank_by_validation_only'],
                      'rule':x['rule'],'train':b(x['train']),
                      'validation':b(x['validation']),
                      'holdout':b(x['holdout'])} for x in entries]},ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__':
    main()
