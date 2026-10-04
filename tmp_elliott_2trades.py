import requests, math, json
from datetime import datetime, timezone

BASE="https://openapi.blofin.com"
PIV=8
MIN_SCORE=55.0
MIN_SWING_ATR=0.5
MAX_PIV=60
BAR_MS=15*60*1000

TRADES=[
    {"inst":"SAND-USDT","side":"SHORT","signal_close_ms":1790946900000,
     "expected":[0.0675,0.06923,0.06619,0.06687]},
    {"inst":"MAGIC-USDT","side":"LONG","signal_close_ms":1790954100000,
     "expected":[0.06115,0.06448,0.06115,0.06448]},
]

def fetch(inst):
    r=requests.get(BASE+"/api/v1/market/candles",
        params={"instId":inst,"bar":"15m","limit":"1440"},
        headers={"Accept":"application/json","User-Agent":"Mozilla/5.0 ElliottCheck/1.0"},timeout=30)
    r.raise_for_status()
    p=r.json()
    if str(p.get("code"))!="0":
        raise RuntimeError(p)
    out=[]
    for row in p.get("data",[]):
        if len(row)>=9 and str(row[8])=="1":
            out.append({"ts":int(row[0]),"o":float(row[1]),"h":float(row[2]),"l":float(row[3]),"c":float(row[4]),"v":float(row[5])})
    out.sort(key=lambda x:x["ts"])
    return out

def atr14(bars):
    tr=[]
    for i,b in enumerate(bars):
        if i==0: t=b["h"]-b["l"]
        else: t=max(b["h"]-b["l"],abs(b["h"]-bars[i-1]["c"]),abs(b["l"]-bars[i-1]["c"]))
        tr.append(t)
    out=[None]*len(bars)
    if len(bars)>=14:
        prev=sum(tr[:14])/14.0
        out[13]=prev
        for i in range(14,len(bars)):
            prev=(prev*13.0+tr[i])/14.0
            out[i]=prev
    return out

def score(x,ideal,width):
    return max(0.0,1.0-abs(x-ideal)/width)

class Radar:
    def __init__(self,bars,atr):
        self.bars=bars; self.atr=atr
        self.pP=[]; self.pB=[]; self.pT=[]
        self.cType="NONE"; self.cScore=0.0; self.cDir=0; self.inv=None
        self.impDir=0; self.impEndBar=None; self.impEndPrice=None

    def extreme(self,ref_bar,price,bar_idx,typ,current):
        best=price; bestbar=bar_idx
        for k in range(ref_bar+1,current+1):
            val=self.bars[k]["h"] if typ==1 else self.bars[k]["l"]
            if (typ==1 and val>best) or (typ==-1 and val<best):
                best=val; bestbar=k
        return best,bestbar

    def push(self,bar_idx,price,typ,min_sw,current):
        n=len(self.pT); changed=False
        if n>0 and self.pT[-1]==typ:
            ref=self.pB[-2] if n>1 else self.pB[-1]
            best,bbar=self.extreme(ref,price,bar_idx,typ,current)
            last=self.pP[-1]
            more=best>last if typ==1 else best<last
            if more:
                self.pP[-1]=best; self.pB[-1]=bbar; changed=True
        else:
            best,bbar=price,bar_idx
            if n>0:
                best,bbar=self.extreme(self.pB[-1],price,bar_idx,typ,current)
            ok=True
            if n>0 and min_sw>0:
                ok=abs(best-self.pP[-1])>=min_sw
            if ok:
                self.pP.append(best); self.pB.append(bbar); self.pT.append(typ); changed=True
        while len(self.pT)>MAX_PIV:
            self.pP.pop(0); self.pB.pop(0); self.pT.pop(0)
        return changed

    def p(self,back): return self.pP[-1-back]
    def b(self,back): return self.pB[-1-back]
    def t(self,back): return self.pT[-1-back]

    def eval_imp(self):
        if len(self.pT)<6:return False,0
        d=self.t(0); p0,p1,p2,p3,p4,p5=[self.p(x) for x in [5,4,3,2,1,0]]
        w1,w2,w3,w4,w5=map(abs,[p1-p0,p2-p1,p3-p2,p4-p3,p5-p4])
        r1=p2>p0 if d==1 else p2<p0
        r2=not(w3<w1 and w3<w5)
        r3=p4>p1 if d==1 else p4<p1
        rp=(p3>p1 and p5>p3) if d==1 else (p3<p1 and p5<p3)
        if r1 and r2 and r3 and rp and w1>0 and w3>0:
            rr2=w2/w1; e3=w3/w1; rr4=w4/w3; e5=w5/w1
            s2=score(rr2,.585,.40)
            s3=max(score(e3,1.618,1.20),.85*score(e3,2.618,1.20))
            s4=score(rr4,.382,.35)
            s5=max(score(e5,.618,.50),score(e5,1.0,.50))
            salt=1.0 if abs(rr2-rr4)>=.10 else abs(rr2-rr4)/.10
            return True,100*(.20*s2+.30*s3+.15*s4+.20*s5+.15*salt)
        return False,0

    def eval_abc(self):
        if len(self.pT)<4:return False,0
        d=self.t(0); p0,pA,pB,pC=[self.p(x) for x in [3,2,1,0]]
        wA,wB,wC=abs(pA-p0),abs(pB-pA),abs(pC-pB)
        if wA>0:
            rB=wB/wA;rC=wC/wA
            bok=.236<=rB<=.886
            cok=pC>pA if d==1 else pC<pA
            clen=.5<=rC<=2.0
            if bok and cok and clen:
                sB=score(rB,.550,.35)
                sC=max(score(rC,1.0,.60),.80*score(rC,1.618,.60))
                return True,100*(.40*sB+.60*sC)
        return False,0

    def eval_flat(self):
        if len(self.pT)<4:return False,0
        d=self.t(0); p0,pA,pB,pC=[self.p(x) for x in [3,2,1,0]]
        wA,wB,wC=abs(pA-p0),abs(pB-pA),abs(pC-pB)
        if wA>0:
            rB=wB/wA;rC=wC/wA
            bok=.90<=rB<=1.38
            cok=pC>pA if d==1 else pC<pA
            clen=.8<=rC<=1.8
            if bok and cok and clen:
                sB=max(score(rB,1.0,.20),.90*score(rB,1.27,.25))
                sC=max(score(rC,1.0,.50),.85*score(rC,1.618,.50))
                return True,100*(.45*sB+.55*sC)
        return False,0

    def eval_devc(self):
        if self.impDir==0 or self.impEndBar is None or len(self.pT)<3:return False,0
        if self.b(2)==self.impEndBar and self.t(2)==self.impDir:
            pA=self.p(1);pB=self.p(0);wA=abs(pA-self.p(2));wB=abs(pB-pA)
            if wA>0:
                rB=wB/wA
                binside=pB<self.impEndPrice if self.impDir==1 else pB>self.impEndPrice
                if binside and .236<=rB<=.886:
                    return True,100*score(rB,.550,.35)
        return False,0

    def eval_dev2(self):
        if len(self.pT)<3:return False,0
        d=self.t(1);p0,p1,p2=[self.p(x) for x in [2,1,0]]
        w1=abs(p1-p0);w2=abs(p2-p1)
        if w1>0:
            rr2=w2/w1;r1=p2>p0 if d==1 else p2<p0
            if r1 and .236<=rr2<1.0:
                return True,100*score(rr2,.585,.45)
        return False,0

    def eval_dev4(self):
        if len(self.pT)<5:return False,0
        d=self.t(1);p0,p1,p2,p3,p4=[self.p(x) for x in [4,3,2,1,0]]
        w1,w2,w3,w4=abs(p1-p0),abs(p2-p1),abs(p3-p2),abs(p4-p3)
        if w1>0 and w3>0:
            r1=p2>p0 if d==1 else p2<p0
            r3=p4>p1 if d==1 else p4<p1
            rp=p3>p1 if d==1 else p3<p1
            w3ok=w3/w1>=.8
            if r1 and r3 and rp and w3ok:
                rr2=w2/w1;e3=w3/w1;rr4=w4/w3
                s2=score(rr2,.585,.40)
                s3=max(score(e3,1.618,1.20),.85*score(e3,2.618,1.20))
                s4=score(rr4,.382,.35)
                return True,100*(.35*s2+.35*s3+.30*s4)
        return False,0

    def select(self):
        vals=[]
        for typ,fun,bonus in [("IMP",self.eval_imp,15),("ABC",self.eval_abc,10),("FLAT",self.eval_flat,10),("DEVC",self.eval_devc,8),("DEV4",self.eval_dev4,5),("DEV2",self.eval_dev2,0)]:
            v,s=fun()
            if v and s>=MIN_SCORE: vals.append((s+bonus,s,typ))
        if not vals:
            self.cType="NONE";self.cScore=0;self.cDir=0;self.inv=None;return
        vals.sort(reverse=True)
        _,s,t=vals[0];self.cType=t;self.cScore=s
        if t=="IMP":
            self.cDir=self.t(0);self.inv=None
            self.impDir=self.cDir;self.impEndBar=self.b(0);self.impEndPrice=self.p(0)
        elif t in ("ABC","FLAT"):
            self.cDir=self.t(0);self.inv=None
        elif t=="DEVC":
            self.cDir=-self.impDir;self.inv=self.impEndPrice
        elif t=="DEV4":
            self.cDir=self.t(1);self.inv=self.p(3)
        elif t=="DEV2":
            self.cDir=self.t(1);self.inv=self.p(2)

    def step(self,i):
        new=False
        if i>=2*PIV:
            j=i-PIV
            lo=max(0,j-PIV);hi=min(len(self.bars)-1,j+PIV)
            win=self.bars[lo:hi+1]
            ph=self.bars[j]["h"] if self.bars[j]["h"]==max(x["h"] for x in win) else None
            pl=self.bars[j]["l"] if self.bars[j]["l"]==min(x["l"] for x in win) else None
            minsw=(self.atr[i] or 0.0)*MIN_SWING_ATR
            if ph is not None and self.push(j,ph,1,minsw,i):new=True
            if pl is not None and self.push(j,pl,-1,minsw,i):new=True
        if new:self.select()
        if self.cType in ("DEV2","DEV4","DEVC") and self.inv is not None:
            b=self.bars[i]
            hit=(self.cDir==1 and b["l"]<self.inv) or (self.cDir==-1 and b["h"]>self.inv)
            if hit:
                self.cType="NONE";self.cScore=0;self.cDir=0;self.inv=None

def run(t):
    bars=fetch(t["inst"])
    target_ts=t["signal_close_ms"]-BAR_MS
    idx=next((i for i,b in enumerate(bars) if b["ts"]==target_ts),None)
    if idx is None:
        print("RESULT",json.dumps({"inst":t["inst"],"error":"signal candle not found","bars":len(bars),"first":bars[0]["ts"] if bars else None,"last":bars[-1]["ts"] if bars else None}))
        return
    a=atr14(bars)
    rd=Radar(bars,a)
    for i in range(idx+1): rd.step(i)
    b=bars[idx]
    expected=t["expected"]
    got=[b["o"],b["h"],b["l"],b["c"]]
    match=all(abs(x-y)<=max(1e-9,abs(y)*1e-8) for x,y in zip(got,expected))
    wanted_dir=1 if t["side"]=="LONG" else -1
    allowed=rd.cType in ("DEV2","DEV4","DEVC") and rd.cDir==wanted_dir and rd.cScore>=MIN_SCORE
    out={
      "inst":t["inst"],"side":t["side"],
      "signal_close_utc":datetime.fromtimestamp(t["signal_close_ms"]/1000,tz=timezone.utc).isoformat(),
      "bars_downloaded":len(bars),"bars_before_and_including_signal":idx+1,
      "ohlc":got,"expected_ohlc":expected,"ohlc_exact_match":match,
      "elliott_type":rd.cType,"elliott_score":round(rd.cScore,4),
      "elliott_dir":"LONG" if rd.cDir==1 else "SHORT" if rd.cDir==-1 else "NONE",
      "pivots_stored":len(rd.pT),"invalidation":rd.inv,
      "FILTER_ALLOWS_TRADE":allowed,
      "FILTER_BLOCKS_TRADE":not allowed
    }
    print("RESULT",json.dumps(out,sort_keys=True))

for t in TRADES:
    try: run(t)
    except Exception as e:
        print("RESULT",json.dumps({"inst":t["inst"],"error":type(e).__name__+": "+str(e)}))
