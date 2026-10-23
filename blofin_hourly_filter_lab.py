#!/usr/bin/env python3
"""Read-only hourly filter research: frozen entry features, no order placement."""
import argparse
import itertools
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("research/blofin_hourly_filter_lab")
FORBIDDEN = ("secret","token","password","api_key","signature","passphrase",
             "order_id","history_id","client_order","exit","closed_","peak",
             "realized","profit","fee_","pnl","after_exit","future","chart",
             "url","endpoint","file","error","message","reason")
MAX_BASE = 48
MAX_DEPTH = 4
MIN_WINS = 3

def number(x):
    try:
        f=float(x)
        return f if math.isfinite(f) else None
    except (ValueError,TypeError,OverflowError):
        return None

def flatten(s):
    """Only data in the entry snapshot, never close results or future markers."""
    d={}
    def walk(v,k="",level=0):
        if level>7: return
        if isinstance(v,dict):
            for name,x in v.items():
                low=str(name).lower()
                if (any(z in low for z in FORBIDDEN) or low.endswith(("_ms","_ts"))
                    or low in ("inst","coin","symbol","strategy","ranked_coins",
                               "top200","name","id","side")): continue
                walk(x,k+"."+str(name) if k else str(name),level+1)
        elif isinstance(v,list):
            if len(v)<=12:
                for i,x in enumerate(v): walk(x,f"{k}[{i}]",level+1)
        elif isinstance(v,bool): d[k]=int(v)
        elif isinstance(v,(int,float)):
            if math.isfinite(v): d[k]=float(v)
        elif isinstance(v,str):
            f=number(v)
            if f is not None: d[k]=f
            elif v.isupper() and 1<len(v)<=16: d[k]=v
    walk(s)
    close=number(s.get("close"))
    if close and close>0:
        for k in ("sma10","sma20"):
            ma=number(s.get(k))
            if ma and ma>0: d["derived.close_vs_"+k+"_pct"]=100*(close/ma-1)
    for x,y,z in (("stoch_k","stoch_d","stoch_k_minus_d"),
                  ("macd_hist","macd_hist_previous","macd_change"),
                  ("rsi14","rsi14_previous","rsi_change")):
        a,b=number(s.get(x)),number(s.get(y))
        if a is not None and b is not None: d["derived."+z]=a-b
    vol,avg=number(s.get("signal_volume")),number(s.get("signal_mean20_prev"))
    if vol is not None and avg and avg>0:
        d["derived.volume_over_ma20"]=vol/avg
    return d

def entry_ok(snapshot,opened):
    if not isinstance(snapshot,dict): return False
    signal=int(snapshot.get("signal_close_ms") or 0)
    if not 0<signal<=opened: return False
    m=snapshot.get("indicator_signal_close_ms")
    if m is not None and int(m)>opened: return False
    def valid(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if k in ("confirmed_close_ms","pivot_confirmed_ms"):
                    n=number(x)
                    if n is not None and n>signal: return False
                if not valid(x): return False
        elif isinstance(v,list):
            return all(valid(x) for x in v)
        return True
    return valid(snapshot)

def load(path):
    state=json.loads(Path(path).read_text(encoding="utf-8"))
    trades=[]; errors=Counter(); seen=set()
    for t in state.get("trade_history",[]):
        try:
            op,cl=int(t["opened_ms"]),int(t["closed_ms"])
            if not (0<op<cl): raise ValueError("no verified close")
            snap=t.get("signal_snapshot") or {}
            if not entry_ok(snap,op): raise ValueError("invalid or missing entry snapshot")
            gross,fee,net=(number(t[k]) for k in ("gross_pnl_usdt","fee_usdt","net_pnl_usdt"))
            notional=number(t.get("notional_usdt"))
            if any(v is None for v in (gross,fee,net,notional)) or notional<=0:
                raise ValueError("missing pnl / notional")
            if abs(net-gross-fee)>1e-6: raise ValueError("inconsistent fees")
            ident=":".join(str(t.get(k) or "") for k in ("history_id","inst","opened_ms","closed_ms"))
            if ident in seen: errors["duplicate"]+=1; continue
            seen.add(ident)
            trades.append(dict(id=ident,opened_ms=op,closed_ms=cl,coin=t.get("inst"),
                side=t.get("side"),net=net,gross=gross,fee=fee,notional=notional,
                rate=net/notional,features=flatten(snap),
                snapshot_strategy=str(snap.get("strategy") or "")))
        except (KeyError,ValueError,TypeError,OverflowError) as exc:
            errors[str(exc)[:60]]+=1
    trades.sort(key=lambda t:(t["opened_ms"],t["id"]))
    return trades,dict(errors)

def candidate_predicates(train):
    fields=sorted(set().union(*(set(r["features"]) for r in train)))
    result=[]
    for key in fields:
        vals=[r["features"][key] for r in train if key in r["features"]]
        if len(vals)<max(5,len(train)//10): continue
        if all(isinstance(x,(float,int)) for x in vals):
            unique=sorted(set(vals))
            if len(unique)<3: continue
            q=sorted(set(unique[round((len(unique)-1)*p)] for p in (.2,.4,.6,.8)))
            for v in q:
                result.extend([dict(key=key,op="<=",value=v),
                               dict(key=key,op=">",value=v)])
            if len(q)>=3: result.append(dict(key=key,op="between",value=[q[0],q[-1]]))
        else:
            for v in sorted(set(str(x) for x in vals))[:8]:
                result.append(dict(key=key,op="==",value=v))
    return result

def matches(row,p):
    v=row["features"].get(p["key"])
    if v is None: return False
    op,t=p["op"],p["value"]
    if op=="==": return v==t
    if not isinstance(v,(int,float)): return False
    if op=="<=": return v<=t
    if op==">": return v>t
    return t[0]<=v<=t[1]

def bitmask(rows,p):
    return sum(1<<i for i,r in enumerate(rows) if matches(r,p))

def freeze_bank(train,max_base=MAX_BASE):
    raw=candidate_predicates(train)
    win=sum(1<<i for i,r in enumerate(train) if r["net"]>0)
    loss=sum(1<<i for i,r in enumerate(train) if r["net"]<0)
    viable=[]; seen=set()
    for p in raw:
        bits=bitmask(train,p)
        w=(bits&win).bit_count()
        l=(bits&loss).bit_count()
        if bits in seen or w<2 or bits.bit_count()==len(train): continue
        seen.add(bits)
        gain=sum(r["net"] for i,r in enumerate(train) if bits>>i&1)
        viable.append((l,-w,-gain,p,bits))
    viable.sort(key=lambda t:(t[0],t[1],t[2],t[3]["key"]))
    chosen=[]; per=Counter()
    for _,_,_,p,_ in viable:
        if per[p["key"]]>=2: continue
        chosen.append(p); per[p["key"]]+=1
        if len(chosen)>=max_base: break
    return chosen,dict(raw_predicates=len(raw),viable=len(viable),
                       frozen_predicates=len(chosen),training_trades=len(train))

def aggregate(rows,bits):
    r=[t for i,t in enumerate(rows) if bits>>i&1]
    return dict(positions=len(r),wins=sum(t["net"]>0 for t in r),
        losses=sum(t["net"]<0 for t in r),flat=sum(t["net"]==0 for t in r),
        net_usdt=round(sum(t["net"] for t in r),8),
        fees_usdt=round(sum(t["fee"] for t in r),8))

def portfolio20(rows,bits):
    bankroll=20.; busy=0; w=l=skip=0
    for i,t in enumerate(rows):
        if not(bits>>i&1): continue
        if t["opened_ms"]<busy: skip+=1; continue
        if bankroll<=0: break
        profit=min(20.,bankroll)*t["rate"]
        bankroll+=profit; busy=t["closed_ms"]
        w+=profit>1e-12; l+=profit< -1e-12
    return dict(wins=w,losses=l,skipped_capital=skip,
        model_roi_pct=round((bankroll/20.-1)*100,5),
        model_pnl_usdt=round(bankroll-20.,7))

def scan_bank(rows,base,freeze_ms,depth=MAX_DEPTH):
    n=len(rows)
    train_ids=[i for i,r in enumerate(rows) if r["closed_ms"]<=freeze_ms]
    fwd_ids=[i for i,r in enumerate(rows) if r["opened_ms"]>freeze_ms]
    train=sum(1<<i for i in train_ids)
    forward=sum(1<<i for i in fwd_ids)
    good=sum(1<<i for i,r in enumerate(rows) if r["net"]>0)
    bad=sum(1<<i for i,r in enumerate(rows) if r["net"]<0)
    pm=[bitmask(rows,p) for p in base]
    best=[]; scanned=0; unique_train=set()
    for k in range(1,min(depth,len(base))+1):
        for indices in itertools.combinations(range(len(base)),k):
            scanned+=1
            m=train|forward
            for i in indices:
                m&=pm[i]
                if not m: break
            tr=m&train
            if not tr: continue
            unique_train.add(tr)
            wins=(tr&good).bit_count()
            losses=(tr&bad).bit_count()
            if wins<MIN_WINS: continue
            if len(best)>=12:
                threshold=best[-1][0]
                rough=(int(losses==0),-losses,wins)
                if rough<threshold[:3]: continue
            pnl=sum(rows[i]["net"] for i in train_ids if tr>>i&1)
            rank=(int(losses==0),-losses,wins,pnl,-k)
            if len(best)>=12 and rank<=best[-1][0]: continue
            best.append((rank,indices)); best.sort(reverse=True)
            if len(best)>12: best.pop()
    selected=[]
    for _,idx in best:
        tr,fw=train,forward
        for i in idx: tr&=pm[i]; fw&=pm[i]
        selected.append(dict(conditions=[base[i] for i in idx],
            training=aggregate(rows,tr),forward=aggregate(rows,fw),
            training_portfolio20=portfolio20(rows,tr),
            forward_portfolio20=portfolio20(rows,fw)))
    return dict(variants_checked=scanned,distinct_training_masks=len(unique_train),
        training_closed=len(train_ids),forward_closed=len(fwd_ids),
        split_boundary_excluded=n-len(train_ids)-len(fwd_ids),
        baseline_training=aggregate(rows,train),baseline_forward=aggregate(rows,forward),
        top_frozen_training_filters=selected)

def main():
    a=argparse.ArgumentParser()
    a.add_argument("--state",default="blofin_live_state.json")
    a.add_argument("--out",default=str(OUT))
    a.add_argument("--now-ms",type=int)
    args=a.parse_args()
    output=Path(args.out); output.mkdir(parents=True,exist_ok=True)
    all_rows,problems=load(args.state)
    cohort="PIVOT_TUNNEL_5M"
    rows=[x for x in all_rows if x["snapshot_strategy"]==cohort]
    problems["other_snapshots_not_pooled"]=len(all_rows)-len(rows)
    now=args.now_ms or round(time.time()*1000)
    rules=output/"frozen_rulebook.json"
    if rules.exists():
        cfg=json.loads(rules.read_text(encoding="utf-8"))
    else:
        train=[r for r in rows if r["closed_ms"]<=now]
        if len(train)<5:
            raise RuntimeError("Less than five verified trades with valid entry snapshots")
        bank,info=freeze_bank(train)
        if not bank:
            raise RuntimeError("Not enough differing entry-time data to build filter bank")
        cfg=dict(version=1,frozen_cutoff_ms=now,predicates=bank,
            creation=info,max_depth=MAX_DEPTH)
        rules.write_text(json.dumps(cfg,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    report=scan_bank(rows,cfg["predicates"],cfg["frozen_cutoff_ms"],cfg["max_depth"])
    prev=output/"latest.json"
    old=json.loads(prev.read_text()) if prev.exists() else {}
    last=max(int(old.get("last_seen_closed_ms") or 0),cfg["frozen_cutoff_ms"])
    fresh=[r for r in rows if r["opened_ms"]>cfg["frozen_cutoff_ms"] and r["closed_ms"]>last]
    newest=max([last]+[r["closed_ms"] for r in rows])
    report.update(generated_utc=datetime.fromtimestamp(now/1000,timezone.utc).isoformat(),
        state="RESEARCH_ONLY_NO_LIVE_CHANGES",entries_analyzed=len(rows),
        all_valid_trade_records=len(all_rows),cohort=cohort,
        excluded_data=problems,base_predicates=len(cfg["predicates"]),
        rulebook_frozen_at_utc=datetime.fromtimestamp(cfg["frozen_cutoff_ms"]/1000,timezone.utc).isoformat(),
        last_seen_closed_ms=newest,
        new_closed=dict(count=len(fresh),wins=sum(r["net"]>0 for r in fresh),
            losses=sum(r["net"]<0 for r in fresh),
            net_usdt=round(sum(r["net"] for r in fresh),7),
            last_coins=[dict(coin=r["coin"],side=r["side"],net_usdt=r["net"]) for r in fresh[-12:]]),
        limitations=["Bounded bank only, NOT all 114-variable combinations",
                     "No lookahead; new trades never tune historical thresholds",
                     "Selected historical trade filtering != full counterfactual simulation",
                     "Portfolio is approximate; no additional trades opened by freed capital",
                     "Other strategy versions are counted, but not pooled into tunnel filters"])
    prev.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    f=report["baseline_forward"]; n=report["new_closed"]; top=report["top_frozen_training_filters"]
    lines=[f'BloFin hourly research | {report["generated_utc"][:16]} UTC',
        f'New trades: {n["count"]} | wins {n["wins"]}, losses {n["losses"]} | {n["net_usdt"]:+.5f} USDT',
        f'Forward since freeze: {report["forward_closed"]} | wins {f["wins"]}, losses {f["losses"]} | {f["net_usdt"]:+.5f} USDT',
        f'Variants examined: {report["variants_checked"]:,} | up to {cfg["max_depth"]} conditions.']
    if top:
        t=top[0]["training"]; v=top[0]["forward"]
        lines.extend([f'Best historic: W {t["wins"]}/L {t["losses"]}; net {t["net_usdt"]:+.5f} USDT',
            f'Same filter forward: W {v["wins"]}/L {v["losses"]}; net {v["net_usdt"]:+.5f} USDT',
            'Rules: ' + "; ".join(str(p["key"])+p["op"]+str(p["value"]) for p in top[0]["conditions"])[:280]])
    lines.append("Analysis only; no confirmed zero-loss system; LIVE unchanged.")
    (output/"latest.txt").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print("\n".join(lines))

if __name__=="__main__":
    main()
