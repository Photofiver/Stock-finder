"""Read-only: LONG fill when price touches the already-confirmed rising lower 5m pivot-tunnel line.
Target +0.5% net after estimated 0.12% round-trip fees. No credentials/orders/state writes.
Conservative: same-bar high cannot establish TP after an intrabar touch (OHLC order unknown).
Exit at tunnel invalidation (confirmed 5m close under latest confirmed higher low).
"""
import concurrent.futures,copy,json,time,statistics
from collections import Counter
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from blofin_pivot_tunnel import evaluate_tunnel,pivot_exit_transition,FIVE
import research_ma7_last24_readonly as m

END=int(time.time()*1000)//FIVE*FIVE
START=END-24*60*60*1000
m.END=END;m.START=START;m.FIRST=START-12*60*60*1000
FEE_PCT=0.12
GROSS_TP_PCT=0.62

def one_coin(inst,bars):
    rows={int(k):tuple(v) for k,v in bars.items()}
    times=sorted(t for t in rows if START<=t<END)
    position=None
    trades=[]
    events=Counter()
    for t in times:
        o,h,l,c,v=rows[t]
        end=t+FIVE
        if position is not None:
            if h >= position['tp']:
                price=position['tp'];outcome='TP';reason='TP_0_5_NET'
            elif pivot_exit_transition(position['pivot'],rows,end,'LONG'):
                price=c;outcome='CLOSE';reason='5M_TUNNEL_INVALIDATED'
            else:
                continue
            result=100*(price/position['entry']-1)-FEE_PCT
            position.update(exit_ms=end,exit_price=price,net_pct=result,
                            gross_pct=result+FEE_PCT,outcome=outcome,reason=reason,
                            hold_min=(end-position['entry_ms'])/60000)
            trades.append(position)
            events[reason]+=1
            position=None
            continue # never re-enter on same candle after exit
        if t<=START:continue
        tunnel=evaluate_tunnel(rows,t)
        if tunnel.get('status')!='VALID' or tunnel.get('tunnel_direction')!='LONG':
            continue
        lower=float(tunnel['lower_price'])
        upper=float(tunnel['upper_price'])
        previous_close=float(rows[t-FIVE][3]) if t-FIVE in rows else float('nan')
        if not(lower<=previous_close<=upper):continue
        # A resting buy limit at the projected lower line, evaluated using only 
        # already-confirmed pivots. Exclude gap-through opens from assumed fills.
        if not (lower<=o<=upper and l<=lower<=h):
            continue
        events['lower_line_touches']+=1
        position={
            'inst':inst,'entry_ms':t,'entry':lower,
            'tp':lower*(1+GROSS_TP_PCT/100),
            'pivot':{'last_confirmed_high':copy.deepcopy(tunnel['last_confirmed_high']),
                     'last_confirmed_low':copy.deepcopy(tunnel['last_confirmed_low'])},
            'entry_bar_same_bar_target_possible':h>=lower*(1+GROSS_TP_PCT/100),
            'signal_upper':upper
        }
        # Exit at confirmed 5m break after entry; never award same-candle TP
        # because the candle's high may have been recorded before the touch.
        if pivot_exit_transition(position['pivot'],rows,end,'LONG'):
            price=c; result=100*(price/lower-1)-FEE_PCT
            position.update(exit_ms=end,exit_price=price,net_pct=result,
                            gross_pct=result+FEE_PCT,outcome='CLOSE',
                            reason='5M_TUNNEL_INVALIDATED',
                            hold_min=5)
            trades.append(position)
            events['5M_TUNNEL_INVALIDATED']+=1
            position=None
    if position is not None:
        position.update(outcome='OPEN',reason='NO_EXIT_BY_END',exit_ms=None,net_pct=None,gross_pct=None)
        trades.append(position)
    return trades,dict(events)

def main():
    state=json.loads(Path('blofin_live_state.json').read_text())
    coins=list(dict.fromkeys(state.get('last_top200') or []))
    if len(coins)!=200:raise RuntimeError('Expected 200 last_top200 coins, found '+str(len(coins)))
    results=[];errors={};event_totals=Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fs={pool.submit(m.candles,coin):coin for coin in coins}
        for index,f in enumerate(concurrent.futures.as_completed(fs),1):
            inst=fs[f]
            try:
                bars=f.result()
                trades,counts=one_coin(inst,bars)
                results+=trades;event_totals.update(counts)
            except Exception as e:
                errors[inst]=f'{type(e).__name__}: {e}'
            if index%40==0:
                print('TOUCH_PROGRESS='+json.dumps({'processed':index,'valid':index-len(errors),'errors':len(errors),'trades':len(results)}),flush=True)
    closed=[x for x in results if x['net_pct'] is not None]
    won=[x for x in closed if x['net_pct']>0]
    lost=[x for x in closed if x['net_pct']<0]
    flats=[x for x in closed if x['net_pct']==0]
    open_=[x for x in results if x['net_pct'] is None]
    summary={
       'start_uk':datetime.fromtimestamp(START/1000,ZoneInfo('Europe/London')).isoformat(),
       'end_uk':datetime.fromtimestamp(END/1000,ZoneInfo('Europe/London')).isoformat(),
       'universe':'fixed last_top200 from pinned reference state 2026-10-21',
       'complete_coins':len(coins)-len(errors),
       'failed_count':len(errors),
       'failed_examples':dict(list(errors.items())[:5]),
       'total_touch_entries':len(results),'wins':len(won),'losses':len(lost),
       'flats':len(flats),'open':len(open_),
       'tp_hits':sum(t['outcome']=='TP' for t in closed),
       'stopped_by_5m_tunnel':sum(t['reason']=='5M_TUNNEL_INVALIDATED' for t in closed),
       'win_rate_closed_pct':round(len(won)/len(closed)*100,2) if closed else None,
       'sum_net_pct_per_trade':round(sum(t['net_pct'] for t in closed),4),
       'mean_net_pct_per_closed_trade':round(statistics.mean(t['net_pct'] for t in closed),4) if closed else None,
       'median_hold_minutes':statistics.median(t['hold_min'] for t in closed) if closed else None,
       'same_entry_bar_tp_order_unknown':sum(t['entry_bar_same_bar_target_possible'] for t in results),
       'assumed_roundtrip_fee_pct':FEE_PCT,'gross_target_pct':GROSS_TP_PCT,
       'net_target_pct':GROSS_TP_PCT-FEE_PCT,
       'limitations':'Hypothetical independent 200-coin limit-order fills; no bid/ask, slippage, tick alignment, actual fills, TOP200 membership changes, max-4 shared bankroll, or intra-5m timing. TP on touch entry bar deliberately ignored (ambiguous). Only closed 5m price pivots before entry; tunnel invalidation at close. Unresolved positions remain OPEN, not winners.'
    }
    print('TOUCH_LONG_RESULT='+json.dumps(summary,ensure_ascii=False),flush=True)
    Path('ma7_tunnel24_results.json').write_text(json.dumps({'summary':summary,'trades':results},ensure_ascii=False,separators=(',',':')))
    print('TEST_ONLY_NOT_LIVE',flush=True)
if __name__=='__main__':main()
