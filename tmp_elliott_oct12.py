import json, math, time
import blofin_live_hourly as bot

STATE="blofin_live_state.json"
OUT="tmp_elliott_oct12_result.json"
PIV=8
MIN_SW_ATR=0.5
MIN_SCORE=55.0
TF=15*60*1000

def score(x,ideal,width):
    return max(0.0,1.0-abs(x-ideal)/width)

def atr_rma(b,n=14):
    tr=[0.0]*len(b); out=[None]*len(b)
    for i in range(len(b)):
        if i==0: tr[i]=b[i]["h"]-b[i]["l"]
        else: tr[i]=max(b[i]["h"]-b[i]["l"],abs(b[i]["h"]-b[i-1]["c"]),abs(b[i]["l"]-b[i-1]["c"]))
    if len(b)>=n:
        out[n-1]=sum(tr[:n])/n
        for i in range(n,len(b)):
            out[i]=(out[i-1]*(n-1)+tr[i])/n
    return out

class Radar:
    def __init__(self,b,atr):
        self.b=b; self.atr=atr
        self.p=[] # [price,bar,type]
        self.cType="NONE"; self.cDir=0; self.inv=None
        self.impDir=0; self.impEndBar=None; self.impEndPrice=None
        self.corrDir=0; self.corrEndBar=None; self.corrEndPrice=None

    def extreme(self,ref,price,bar_idx,typ,current):
        best=price; bestbar=bar_idx
        start=max(ref+1,0)
        for j in range(start,current+1):
            v=self.b[j]["h"] if typ==1 else self.b[j]["l"]
            if (typ==1 and v>best) or (typ==-1 and v<best):
                best=v; bestbar=j
        return best,bestbar

    def push(self,bar_idx,price,typ,minsw,current):
        changed=False
        n=len(self.p)
        if n and self.p[-1][2]==typ:
            ref=self.p[-2][1] if n>1 else self.p[-1][1]
            best,bb=self.extreme(ref,price,bar_idx,typ,current)
            if (typ==1 and best>self.p[-1][0]) or (typ==-1 and best<self.p[-1][0]):
                self.p[-1]=[best,bb,typ]; changed=True
        else:
            best,bb=(price,bar_idx)
            if n: best,bb=self.extreme(self.p[-1][1],price,bar_idx,typ,current)
            if (not n) or minsw<=0 or abs(best-self.p[-1][0])>=minsw:
                self.p.append([best,bb,typ]); changed=True
        if len(self.p)>60:self.p=self.p[-60:]
        return changed

    def fp(self,back): return self.p[-1-back][0]
    def fb(self,back): return self.p[-1-back][1]
    def ft(self,back): return self.p[-1-back][2]

    def eval_imp(self):
        if len(self.p)<6:return (False,0)
        d=self.ft(0); p0,p1,p2,p3,p4,p5=[self.fp(x) for x in [5,4,3,2,1,0]]
        w1,w2,w3,w4,w5=map(abs,[p1-p0,p2-p1,p3-p2,p4-p3,p5-p4])
        r1=p2>p0 if d==1 else p2<p0
        r2=not(w3<w1 and w3<w5)
        r3=p4>p1 if d==1 else p4<p1
        rp=(p3>p1 and p5>p3) if d==1 else (p3<p1 and p5<p3)
        if not(r1 and r2 and r3 and rp and w1>0 and w3>0):return (False,0)
        rr2=w2/w1;e3=w3/w1;rr4=w4/w3;e5=w5/w1
        s2=score(rr2,.585,.40);s3=max(score(e3,1.618,1.20),.85*score(e3,2.618,1.20))
        s4=score(rr4,.382,.35);s5=max(score(e5,.618,.50),score(e5,1.0,.50))
        salt=1.0 if abs(rr2-rr4)>=.10 else abs(rr2-rr4)/.10
        return True,100*(.20*s2+.30*s3+.15*s4+.20*s5+.15*salt)

    def eval_abc(self,flat=False):
        if len(self.p)<4:return (False,0)
        d=self.ft(0); p0,pA,pB,pC=[self.fp(x) for x in [3,2,1,0]]
        wA,wB,wC=abs(pA-p0),abs(pB-pA),abs(pC-pB)
        if wA<=0:return (False,0)
        rB=wB/wA;rC=wC/wA;cok=pC>pA if d==1 else pC<pA
        if not flat:
            ok=(.236<=rB<=.886) and cok and (.5<=rC<=2.0)
            if not ok:return(False,0)
            return True,100*(.40*score(rB,.550,.35)+.60*max(score(rC,1.0,.60),.80*score(rC,1.618,.60)))
        ok=(.90<=rB<=1.38) and cok and (.8<=rC<=1.8)
        if not ok:return(False,0)
        return True,100*(.45*max(score(rB,1.0,.20),.90*score(rB,1.27,.25))+.55*max(score(rC,1.0,.50),.85*score(rC,1.618,.50)))

    def eval_dev2(self):
        if len(self.p)<3:return(False,0)
        d=self.ft(1);p0,p1,p2=[self.fp(x) for x in [2,1,0]]
        w1=abs(p1-p0);w2=abs(p2-p1)
        if w1<=0:return(False,0)
        rr=w2/w1;r1=p2>p0 if d==1 else p2<p0
        return (True,100*score(rr,.585,.45)) if r1 and .236<=rr<1.0 else (False,0)

    def eval_dev4(self):
        if len(self.p)<5:return(False,0)
        d=self.ft(1);p0,p1,p2,p3,p4=[self.fp(x) for x in [4,3,2,1,0]]
        w1,w2,w3,w4=abs(p1-p0),abs(p2-p1),abs(p3-p2),abs(p4-p3)
        if w1<=0 or w3<=0:return(False,0)
        r1=p2>p0 if d==1 else p2<p0;r3=p4>p1 if d==1 else p4<p1;rp=p3>p1 if d==1 else p3<p1
        if not(r1 and r3 and rp and w3/w1>=.8):return(False,0)
        rr2=w2/w1;e3=w3/w1;rr4=w4/w3
        s=100*(.35*score(rr2,.585,.40)+.35*max(score(e3,1.618,1.20),.85*score(e3,2.618,1.20))+.30*score(rr4,.382,.35))
        return True,s

    def eval_devc(self):
        if self.impDir==0 or self.impEndBar is None or len(self.p)<3:return(False,0)
        if self.fb(2)!=self.impEndBar or self.ft(2)!=self.impDir:return(False,0)
        pA,pB=self.fp(1),self.fp(0);wA=abs(pA-self.fp(2));wB=abs(pB-pA)
        if wA<=0:return(False,0)
        rB=wB/wA;inside=pB<self.impEndPrice if self.impDir==1 else pB>self.impEndPrice
        return (True,100*score(rB,.550,.35)) if inside and .236<=rB<=.886 else (False,0)

    def choose(self):
        vals=[]
        for typ,ev,bonus in [("IMP",self.eval_imp(),15),("ABC",self.eval_abc(False),10),("FLAT",self.eval_abc(True),10),("DEVC",self.eval_devc(),8),("DEV4",self.eval_dev4(),5),("DEV2",self.eval_dev2(),0)]:
            if ev[0] and ev[1]>=MIN_SCORE: vals.append((ev[1]+bonus,ev[1],typ))
        if not vals:
            self.cType="NONE";self.cDir=0;self.inv=None;return
        vals.sort(reverse=True);_,raw,t=vals[0];self.cType=t
        if t=="IMP":
            self.cDir=self.ft(0);self.inv=None;self.impDir=self.cDir;self.impEndBar=self.fb(0);self.impEndPrice=self.fp(0)
        elif t in ("ABC","FLAT"):
            self.cDir=self.ft(0);self.inv=None;self.corrDir=self.cDir;self.corrEndBar=self.fb(0);self.corrEndPrice=self.fp(0)
        elif t=="DEVC":
            self.cDir=-self.impDir;self.inv=self.impEndPrice
        elif t=="DEV4":
            self.cDir=self.ft(1);self.inv=self.fp(3)
        elif t=="DEV2":
            self.cDir=self.ft(1);self.inv=self.fp(2)

    def phase_all(self):
        # Prefer validated native developing counts.
        if self.cType=="DEV2": return "3",self.cDir
        if self.cType=="DEV4": return "5",self.cDir
        if self.cType=="DEVC": return "C",self.cDir
        # After a validated completed impulse: A, then B, then C.
        if self.impEndBar is not None:
            idx=next((i for i,x in enumerate(self.p) if x[1]==self.impEndBar),None)
            if idx is not None:
                k=len(self.p)-1-idx
                if k==0:return "A",-self.impDir
                if k==1:return "B", self.impDir
                if k==2:return "C",-self.impDir
        # After a validated completed correction: 1,2,3,4,5.
        if self.corrEndBar is not None:
            idx=next((i for i,x in enumerate(self.p) if x[1]==self.corrEndBar),None)
            if idx is not None:
                k=len(self.p)-1-idx
                if 0<=k<=4:
                    dirs=[-self.corrDir,self.corrDir,-self.corrDir,self.corrDir,-self.corrDir]
                    return str(k+1),dirs[k]
        return None,0

    def run(self):
        n=len(self.b)
        for i in range(n):
            new=False
            if i>=2*PIV and self.atr[i] is not None:
                k=i-PIV
                lo=max(0,k-PIV);hi=min(n-1,k+PIV)
                ph=all(self.b[k]["h"]>=self.b[j]["h"] for j in range(lo,hi+1))
                pl=all(self.b[k]["l"]<=self.b[j]["l"] for j in range(lo,hi+1))
                minsw=MIN_SW_ATR*self.atr[i]
                if ph:new=self.push(k,self.b[k]["h"],1,minsw,i) or new
                if pl:new=self.push(k,self.b[k]["l"],-1,minsw,i) or new
            if new:self.choose()
            if self.cType in ("DEV2","DEV4","DEVC") and self.inv is not None:
                if (self.cDir==1 and self.b[i]["l"]<self.inv) or (self.cDir==-1 and self.b[i]["h"]>self.inv):
                    self.cType="NONE";self.cDir=0;self.inv=None
        return self.cType,self.cDir,self.phase_all()

def fetch_bars(inst,close_ms):
    raw=bot.market_get("/api/v1/market/candles",{"instId":inst,"bar":"15m","after":str(close_ms+TF),"limit":"300"})
    b=bot.parse_candles(raw)
    b=sorted([x for x in b if int(x["ts"])+TF<=close_ms],key=lambda x:x["ts"])
    return b

with open(STATE,"r",encoding="utf-8") as f: st=json.load(f)
rows=[]
for t in st.get("trade_history",[]):
    om=int(t.get("opened_ms") or 0)
    d=time.gmtime(om/1000)
    if not(d.tm_year==2026 and d.tm_mon==10 and d.tm_mday in (1,2)):continue
    tech="SAFETY:" in str(t.get("reason") or "")
    if tech:continue
    close_ms=int(t.get("signal_close_ms") or ((om//TF)*TF))
    try:
        b=fetch_bars(t["inst"],close_ms)
        if len(b)<40: raise RuntimeError("too few bars "+str(len(b)))
        atr=atr_rma(b); rad=Radar(b,atr); ct,cd,(ph,pd)=rad.run()
        side_dir=1 if t["side"]=="LONG" else -1
        native_phase={"DEV2":"3","DEV4":"5","DEVC":"C"}.get(ct)
        native_allow=bool(native_phase and cd==side_dir)
        all_allow=bool(ph and pd==side_dir)
        rows.append({"inst":t["inst"],"side":t["side"],"opened_ms":om,"net":float(t.get("net_pnl_usdt") or 0),"win":float(t.get("net_pnl_usdt") or 0)>0,
                     "radar_type":ct,"radar_dir":cd,"native_phase":native_phase,"native_allow":native_allow,
                     "all_phase":ph,"all_dir":pd,"all_allow":all_allow,"bars":len(b)})
    except Exception as e:
        rows.append({"inst":t["inst"],"side":t["side"],"opened_ms":om,"net":float(t.get("net_pnl_usdt") or 0),"win":float(t.get("net_pnl_usdt") or 0)>0,"error":repr(e)})

valid=[r for r in rows if "error" not in r]
loss=[r for r in valid if not r["win"]];wins=[r for r in valid if r["win"]]
res={
 "total_market_trades":len(rows),"valid":len(valid),"errors":[r for r in rows if "error" in r],
 "losses":len(loss),"wins":len(wins),
 "native_3_5_C":{"losses_blocked":sum(not r["native_allow"] for r in loss),"losses_allowed":sum(r["native_allow"] for r in loss),"wins_kept":sum(r["native_allow"] for r in wins),"wins_blocked":sum(not r["native_allow"] for r in wins)},
 "all_waves":{"losses_blocked":sum(not r["all_allow"] for r in loss),"losses_allowed":sum(r["all_allow"] for r in loss),"wins_kept":sum(r["all_allow"] for r in wins),"wins_blocked":sum(not r["all_allow"] for r in wins)},
 "rows":rows}
with open(OUT,"w",encoding="utf-8") as f:json.dump(res,f,ensure_ascii=False,indent=2)
print(json.dumps(res,ensure_ascii=False))
