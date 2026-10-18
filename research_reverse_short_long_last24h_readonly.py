"""READ ONLY continuous last 24h reverse SHORT into LONG backtest.

Use frozen 390-crypto-futures shard candles from 17-18 October plus
append newly completed public BloFin 5m candles. NO trading endpoints,
API keys, private user account or LIVE file mutations.

At every 5m boundary, calculate TOP200 from all then-available frozen
universe coins on past rolling 288 completed 5m close changes, not
ranking from future winners. EXACT LIVE entry_signal and exit_transition
functions are called. Long exits only on eligible GREEN > last RED;
no TP/SL/time exit, no DI, no lookahead. 4-slot / 25 USDT capital
approximation is separately computed as a *simulation*, not confirmed
exchange orders.
"""
from __future__ import annotations
import collections
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from decimal import Decimal
import json, time, math
from pathlib import Path
from zoneinfo import ZoneInfo
import short60_volume_mean10_24hour_readonly as frozen
import blofin_top200_repeat_setup_research as api
import blofin_reverse_short_long_live as strat

F=300000
UK=ZoneInfo("Europe/London")
START_CAP=25.0
FEE_PCT=.12
MAX_POSITIONS=4
MIN_NOTIONAL=5.0
MAX_PER_POSITION=20.0
CAP_BUFFER=.5
# Caches of 5m dictionaries to feed same LIVE strategy.
def clock(ms):
    return datetime.fromtimestamp(ms/1000,UK).strftime("%Y-%m-%d %H:%M")

def tobars(data,signal_close,lookback=40):
    first=signal_close-lookback*F
    return [{"ts":i,"o":x[0],"h":x[1],"l":x[2],"c":x[3],"v":x[4]}
            for i in range(first,signal_close,F)
            if (x:=data.get(i)) is not None]

def frozen_plus_latest():
    market,old_errors,total=frozen.load()
    if len(market)!=390 or old_errors:
        raise RuntimeError("Frozen shard source changed or incomplete")
    now=int(time.time()*1000)
    end=(now//F)*F
    start=end-288*F
    if not (frozen.START<=start and frozen.END<end and end-frozen.END<=10*60*60*1000):
        raise RuntimeError("Frozen + recent join out of supported range (need Oct 18 evening)")
    print("WINDOW="+json.dumps({"start_uk":clock(start),"end_uk":clock(end),
        "n_bars":288,"frozen_end_uk":clock(frozen.END),
        "public_symbols":len(market)},separators=(",",":")),flush=True)

    # Get 5m bars from precisely around the frozen endpoint through the
    # new endpoint, and never request candles after the frozen new endpoint.
    api.MIN_CALL_GAP=.16
    newer_errors={}
    newer_success=0
    latest_end_ms=end+F
    def fetch_one(inst):
        rows,pages=api.candles(inst,latest_end_ms, frozen.END-3*F)
        # For complete data require every new 5m candle through end exclusive.
        missing=[ts for ts in range(frozen.END,end,F) if ts not in rows]
        if missing:
            raise RuntimeError(f"{len(missing)} new candles unavailable, first={clock(missing[0])}")
        return rows,pages
    with ThreadPoolExecutor(max_workers=9) as pool:
        jobs={pool.submit(fetch_one,inst):inst for inst in market}
        for f in as_completed(jobs):
            inst=jobs[f]
            try:
                bars,_=f.result()
                market[inst].update({ts:row for ts,row in bars.items() if ts>=frozen.END and ts<end})
                newer_success+=1
            except Exception as exc:
                newer_errors[inst]=str(exc)[:160]
            complete=newer_success+len(newer_errors)
            if complete%75==0 or complete==len(market):
                print("LATEST_PROGRESS="+json.dumps({"done":complete,"of":len(market),
                         "complete":newer_success,"failed":len(newer_errors)},separators=(",",":")),flush=True)
    if "BTC-USDT" in newer_errors:
        raise RuntimeError("BTC candle download missing: "+newer_errors["BTC-USDT"])
    if len(newer_errors)>10:
        raise RuntimeError(f"Too many missing market OHLCV series, cannot assert TOP200: {len(newer_errors)}")
    for inst in newer_errors:
        # Do not rank partially-filled coins to avoid false signals.
        market.pop(inst,None)
    return market,start,end,newer_errors,total


class SignalBot:
    SIGNAL_MINUTES=5

def entry(inst,series,btc,ts):
    bars=tobars(series,ts+F,42)
    btcbars=tobars(btc,ts+F,8)
    return strat.entry_signal(SignalBot(),bars,btcbars,ts+F)

def exit_one(pos,series,ts):
    bars=tobars(series,ts+F,15)
    return strat.exit_transition(pos,bars,ts+F)

def source_rankings(market,start,end):
    ranks={}
    coverage=[]
    for ts in range(start-F,end,F):
        previous=ts-288*F
        ordered=[]
        for inst,rows in market.items():
            now=rows.get(ts);old=rows.get(previous)
            if now is not None and old is not None and old[3]>0:
                ordered.append((now[3]/old[3]-1,inst))
        ordered.sort(reverse=True)
        if len(ordered)<200:
            raise RuntimeError(f"Fewer than 200 candidate coins at {clock(ts)}")
        ranks[ts]={inst:rank for rank,(_,inst) in enumerate(ordered[:200],1)}
        coverage.append(len(ordered))
    return ranks,coverage

def collect_candidates(market,ranks,start,end):
    """Across all TOP200 closed candles, no position constraint yet."""
    btc=market["BTC-USDT"]
    reasons=collections.Counter()
    candidates=[]
    tslist=range(start-F,end-F,F)  # closes start..end-5m inclusive; entries >=start and <end
    for ts in tslist:
        toplist=ranks[ts]
        for inst,rank in toplist.items():
            data=market[inst]
            cur=data.get(ts);prev=data.get(ts-F)
            if cur is None or prev is None:
                reasons["no_two_bars"]+=1;continue
            if not (cur[3]<cur[0] and prev[3]>prev[0] and prev[4]>0):
                reasons["not_red_after_green"]+=1;continue
            reasons["red_after_green"]+=1
            ratio=cur[4]/prev[4]
            if not (3<=ratio<4):
                reasons["volume_ratio_not3to4"]+=1;continue
            reasons["volume_ratio_pass"]+=1
            last10=[data.get(ts-i*F) for i in range(1,11)]
            if any(x is None for x in last10):
                reasons["missing_prev10"]+=1;continue
            mean=sum(x[4] for x in last10)/10.
            if mean<=0 or cur[4]>mean:
                reasons["above_volume_mean10"]+=1;continue
            reasons["volume_mean10_pass"]+=1
            old=data.get(ts-30*F)
            if old is None:
                reasons["missing_prior150"]+=1;continue
            if cur[3]<old[3]:
                reasons["prior150_negative"]+=1;continue
            reasons["prior150_pass"]+=1
            sig=strat._completed_coin15_pct(data,ts)
            if sig is None or sig<strat.COIN15_MIN:
                reasons["coin_closed15_block"]+=1;continue
            reasons["coin_closed15_pass"]+=1
            if cur[1]<=prev[1] and cur[2]>=prev[2]:
                reasons["inside_bar"]+=1;continue
            reasons["not_inside_pass"]+=1
            bc=btc.get(ts);bp=btc.get(ts-3*F)
            if bc is None or bp is None:
                reasons["btc_missing"]+=1;continue
            if 100*(bc[3]/bp[3]-1)>strat.BTC15_MAX:
                reasons["btc15_positive_too_high"]+=1;continue
            reasons["all_pass"]+=1
            exact=entry(inst,data,btc,ts)
            if exact is None:
                raise AssertionError(f"Mismatch LIVE entry_signal for {inst} at {clock(ts+F)}")
            if data.get(ts+F) is None:
                reasons["next_open_missing"]+=1;continue
            candidates.append({"coin":inst,"rank":rank,"signal_close_ms":ts+F,
                               "signal_time_uk":clock(ts+F),
                               "signal_bar_open_ms":ts,"entry_ms":ts+F,
                               "entry_price":data[ts+F][0],
                               "volume_ratio":round(ratio,4),
                               "btc15_pct":round(100*(bc[3]/bp[3]-1),4)})
    return candidates,reasons

def simulate(market, candidates,start,end,portfolio=False):
    """Per-coin position lifecycle. Portfolio version additionally enforces
    4 slots, capital $25, $5 min nominal and $0.50 cash buffer; assumes
    full fills at exact next 5m OPEN and no balance loss from unrealized P&L.
    """
    bytime=collections.defaultdict(list)
    for sig in candidates:
        bytime[sig["entry_ms"]].append(sig)
    for arr in bytime.values():
        arr.sort(key=lambda c:(c["rank"],c["coin"]))
    positions={}
    all_entries=[]
    all_closes=[]
    no_budget=0
    already_open=0
    blocked_slots=0
    realised_net_usdt=0.0
    bankroll=START_CAP
    for ts in range(start,end,F):
        # process exit signals on the PREVIOUS completed 5m candle and
        # close at THIS 5m candle open.
        prev=ts-F
        for coin in list(positions):
            p=positions[coin]
            if ts<=p["entry_ms"]:continue
            data=market[coin]
            if data.get(prev) is None or data.get(ts) is None:continue
            should_close,last_red=exit_one(p,data,prev)
            p["last_red_volume"]=last_red
            if not should_close:continue
            exit_price=data[ts][0]
            gross=100*(exit_price/p["entry_price"]-1)
            net=gross-FEE_PCT
            notional=p["notional_usdt"]
            pnl=notional*net/100.
            if portfolio:
                realised_net_usdt+=pnl
                bankroll=max(0.0,bankroll+pnl)
            all_closes.append({
                "coin":coin,"entry_uk":clock(p["entry_ms"]),
                "exit_uk":clock(ts),"gross_pct":round(gross,5),
                "net_pct":round(net,5),"notional_usdt":round(notional,5),
                "estimated_net_pnl_usdt":round(pnl,6),
                "result":"WIN" if net>0 else "LOSS" if net<0 else "FLAT"})
            positions.pop(coin)

        for sig in bytime.get(ts,[]):
            coin=sig["coin"]
            if coin in positions:
                already_open+=1
                continue
            if portfolio:
                if len(positions)>=MAX_POSITIONS:
                    blocked_slots+=1
                    continue
                committed=sum(p["notional_usdt"] for p in positions.values())
                # Real LIVE cap counts INITIAL bankroll +/- realised, and
                # also broker reported available equity; here approximated
                # as available = bankroll - committed with buffer.
                remaining=min(25.0,bankroll)-committed
                free=max(0.0,bankroll-committed)
                spendable=min(remaining,free-CAP_BUFFER)
                if spendable<MIN_NOTIONAL:
                    no_budget+=1
                    continue
                target=min(MAX_PER_POSITION,spendable/2.)
                if target<MIN_NOTIONAL:target=min(MAX_PER_POSITION,spendable)
            else:
                target=20.0
            if not target>0:continue
            pos={"coin":coin,"rank":sig["rank"],"entry_ms":ts,
                 "entry_price":sig["entry_price"],"last_red_volume":market[coin][ts-F][4],
                 "notional_usdt":target}
            positions[coin]=pos
            all_entries.append({**sig,"notional_usdt":round(target,5)})
    wins=sum(x["result"]=="WIN" for x in all_closes)
    losses=sum(x["result"]=="LOSS" for x in all_closes)
    still_open=[{"coin":coin,"entry_uk":clock(p["entry_ms"]),
                 "entry_price":p["entry_price"],"notional_usdt":round(p["notional_usdt"],5)}
                for coin,p in positions.items()]
    return {"entered":len(all_entries),"closed":len(all_closes),
            "wins_after_fees":wins,"losses_after_fees":losses,
            "winrate_after_fees_pct":round(100*wins/(wins+losses),2) if wins+losses else None,
            "still_open":len(still_open),"open_positions":still_open,
            "blocked_already_open_same_coin":already_open,
            "blocked_no_budget":no_budget,
            "blocked_max_slots":blocked_slots,
            "estimated_realised_net_usdt":round(sum(x["estimated_net_pnl_usdt"] for x in all_closes),5),
            "capital_end_estimated_usdt":round(bankroll,5) if portfolio else None,
            "trades":all_entries,"closes":all_closes}

def main():
    market,start,end,errors,total=frozen_plus_latest()
    ranks,coverage=source_rankings(market,start,end)
    candidates,reasons=collect_candidates(market,ranks,start,end)
    unconstrained=simulate(market,candidates,start,end,portfolio=False)
    limited=simulate(market,candidates,start,end,portfolio=True)
    report={
        "window_UK":[clock(start),clock(end)],
        "bars":288,
        "strategy":"REV_SHORT_20W_LONG_VOLUME_V1",
        "exact_live_entry_and_exit_functions":True,
        "universe_current_snapshot_coins":total,
        "coins_with_full_joined_ohlcv":len(market),
        "missing_recent_coin_series":errors,
        "ranking":"Past 288 completed 5m close gain for all available coins, rank TOP200 at every past 5m bar without lookahead. Approximate LIVE exchange 24h last/open24h ranking.",
        "rank_coverage_min":min(coverage),
        "rank_coverage_max":max(coverage),
        "total_full_entry_signals":len(candidates),
        "all_candidate_signals":candidates,
        "first_failing_condition_counts":dict(reasons),
        "continuous_one_position_per_coin_ignore_budget":unconstrained,
        "continuous_portfolio_25usdt_budget_max4_slots":limited,
        "assumptions":["Actual position entry/exit assumed at exact next 5m open; exchange execution delayed by GitHub runners and orderbook stale quote gate.",
            "Estimated trading fees 0.12% roundtrip; no spread/slippage/funding included.",
            "Portfolio 25USDT 1x uses simple notional sizing and min 5USDT; does not simulate contract lot/minSize restrictions, open-order reconciliation, untracked positions or 10% daily drawdown safety latch. It is an idealized upper bound.",
            "Existing open positions before this window omitted; positions still open at end have no realized P&L.",
            "Coin universe frozen at 18:00 UK Oct 18 snapshot; newly listed coins after that may not be included.",
            "Never train/change filter thresholds on backtest outcomes. LIVE strategy unchanged."],
    }
    out=Path("reverse_short_long_last24h_readonly_result.json")
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    summary={"window_uk":report["window_UK"],
        "source_symbols":total,"coverage":len(market),"rank_min":min(coverage),
        "strict_signals":len(candidates),
        "rejection_counts":dict(reasons),
        "per_coin_ideal":{k:v for k,v in unconstrained.items() if k not in ("trades","closes","open_positions")},
        "capital25_portfolio":{k:v for k,v in limited.items() if k not in ("trades","closes","open_positions")},
        "first_signals":candidates[:40],
        "portfolio_entries":limited["trades"][:35],
        "portfolio_closes":limited["closes"][:35],
        "coin_series_errors":errors}
    print("LAST24H_REVERSE_LONG_RESULT="+json.dumps(summary,ensure_ascii=False,separators=(",",":")),flush=True)

if __name__=="__main__":
    main()
