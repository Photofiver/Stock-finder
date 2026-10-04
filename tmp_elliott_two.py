import json, math, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE="https://openapi.blofin.com"
PIV_LEN=8
MIN_SWING_ATR=0.5
MAX_PIV=60
MIN_SCORE=55.0

TESTS=[
    {"inst":"SAND-USDT","side":"SHORT","signal_close_ms":1790946900000},
    {"inst":"MAGIC-USDT","side":"LONG","signal_close_ms":1790954100000},
]

def get_bars(inst):
    q=urllib.parse.urlencode({"instId":inst,"bar":"15m","limit":"1000"})
    req=urllib.request.Request(BASE+"/api/v1/market/candles?"+q,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=30) as r:
        p=json.load(r)
    if str(p.get("code"))!="0":
        raise RuntimeError(p)
    out=[]
    for row in p.get("data",[]):
        if len(row)>=9 and str(row[8])=="1":
            out.append({"ts":int(row[0]),"o":float(row[1]),"h":float(row[2]),"l":float(row[3]),"c":float(row[4]),"v":float(row[5])})
    out.sort(key=lambda x:x["ts"])
    return out

def atr14(b):
    tr=[]
    for i,x in enumerate(b):
        if i==0: t=x["h"]-x["l"]
        else: t=max(x["h"]-x["l"],abs(x["h"]-b[i-1]["c"]),abs(x["l"]-b[i-1]["c"]))
        tr.append(t)
    out=[None]*len(b)
    if len(b)>=14:
        a=sum(tr[:14])/14.0
        out[13]=a
        for i in range(14,len(b)):
            a=(a*13+tr[i])/14.0
            out[i]=a
    return out

def score(x,ideal,width):
    return max(0.0,1.0-abs(x-ideal)/width)

class EW:
    def __init__(self,bars):
        self.b=bars; self.pP=[]; self.pB=[]; self.pT=[]
        self.impDir=0; self.impEndBar=None; self.impEndPrice=None
        self.cType="NONE"; self.cScore=0.0; self.cDir=0; self.inv=None
        self.atr=atr14(bars)
    def extreme(self,ref,price,baridx,typ,cur):
        best=price; bb=baridx
        for idx in range(cur,ref,-1):
            val=self.b[idx]["h"] if typ==1 else self.b[idx]["l"]
            if (typ==1 and val>best) or (typ==-1 and val<best):
                best=val; bb=idx
        return best,bb
    def push(self,baridx,price,typ,minsw,cur):
        changed=False; n=len(self.pT)
        if n and self.pT[-1]==typ:
            ref=self.pB[-2] if n>1 else self.pB[-1]
            best,bb=self.extreme(ref,price,baridx,typ,cur)
            more=best>self.pP[-1] if typ==1 else best<self.pP[-1]
            if more:
                self.pP[-1]=best; self.pB[-1]=bb; changed=True
        else:
            best,bb=price,baridx
            if n: best,bb=self.extreme(self.pB[-1],price,baridx,typ,cur)
            ok=True
            if n and minsw>0: ok=abs(best-self.pP[-1])>=minsw
            if ok:
                self.pP.append(best); self.pB.append(bb); self.pT.append(typ); changed=True
        while len(self.pT)>MAX_PIV:
            self.pP.pop(0); self.pB.pop(0); self.pT.pop(0)
        return changed
    def p(self,k): return self.pP[-1-k]
    def t(self,k): return self.pT[-1-k]
    def bb(self,k): return self.pB[-1-k]
    def impulse(self):
        if len(self.pT)<6:return False,0
        d=self.t(0); p0,p1,p2,p3,p4,p5=[self.p(k) for k in [5,4,3,2,1,0]]
        w1,w2,w3,w4,w5=map(abs,[p1-p0,p2-p1,p3-p2,p4-p3,p5-p4])
        r1=p2>p0 if d==1 else p2<p0
        r2=not(w3<w1 and w3<w5)
        r3=p4>p1 if d==1 else p4<p1
        rp=(p3>p1 and p5>p3) if d==1 else (p3<p1 and p5<p3)
        if r1 and r2 and r3 and rp and w1>0 and w3>0:
            rr2=w2/w1;e3=w3/w1;rr4=w4/w3;e5=w5/w1
            s2=score(rr2,.585,.40); s3=max(score(e3,1.618,1.20),.85*score(e3,2.618,1.20))
            s4=score(rr4,.382,.35); s5=max(score(e5,.618,.50),score(e5,1,.50))
            salt=1.0 if abs(rr2-rr4)>=.10 else abs(rr2-rr4)/.10
            return True,100*(.20*s2+.30*s3+.15*s4+.20*s5+.15*salt)
        return False,0
    def abc(self):
        if len(self.pT)<4:return False,0
        d=self.t(0); p0,pA,pB,pC=[self.p(k) for k in [3,2,1,0]]
        wA,wB,wC=abs(pA-p0),abs(pB-pA),abs(pC-pB)
        if wA>0:
            rB=wB/wA;rC=wC/wA
            if .236<=rB<=.886 and (pC>pA if d==1 else pC<pA) and .5<=rC<=2:
                return True,100*(.40*score(rB,.550,.35)+.60*max(score(rC,1,.60),.80*score(rC,1.618,.60)))
        return False,0
    def flat(self):
        if len(self.pT)<4:return False,0
        d=self.t(0); p0,pA,pB,pC=[self.p(k) for k in [3,2,1,0]]
        wA,wB,wC=abs(pA-p0),abs(pB-pA),abs(pC-pB)
        if wA>0:
            rB=wB/wA;rC=wC/wA
            if .90<=rB<=1.38 and (pC>pA if d==1 else pC<pA) and .8<=rC<=1.8:
                return True,100*(.45*max(score(rB,1,.20),.90*score(rB,1.27,.25))+.55*max(score(rC,1,.50),.85*score(rC,1.618,.50)))
        return False,0
    def devc(self):
        if self.impDir and self.impEndBar is not None and len(self.pT)>=3 and self.bb(2)==self.impEndBar and self.t(2)==self.impDir:
            pA,pB=self.p(1),self.p(0); wA=abs(pA-self.p(2));wB=abs(pB-pA)
            if wA>0:
                r=wB/wA; inside=pB<self.impEndPrice if self.impDir==1 else pB>self.impEndPrice
                if inside and .236<=r<=.886:return True,100*score(r,.550,.35)
        return False,0
    def dev2(self):
        if len(self.pT)<3:return False,0
        d=self.t(1); p0,p1,p2=[self.p(k) for k in [2,1,0]];w1=abs(p1-p0);w2=abs(p2-p1)
        if w1>0:
            r=w2/w1; r1=p2>p0 if d==1 else p2<p0
            if r1 and .236<=r<1:return True,100*score(r,.585,.45)
        return False,0
    def dev4(self):
        if len(self.pT)<5:return False,0
        d=self.t(1);p0,p1,p2,p3,p4=[self.p(k) for k in [4,3,2,1,0]]
        w1,w2,w3,w4=abs(p1-p0),abs(p2-p1),abs(p3-p2),abs(p4-p3)
        if w1>0 and w3>0:
            r1=p2>p0 if d==1 else p2<p0; r3=p4>p1 if d==1 else p4<p1; rp=p3>p1 if d==1 else p3<p1
            if r1 and r3 and rp and w3/w1>=.8:
                rr2=w2/w1;e3=w3/w1;rr4=w4/w3
                s2=score(rr2,.585,.40);s3=max(score(e3,1.618,1.20),.85*score(e3,2.618,1.20));s4=score(rr4,.382,.35)
                return True,100*(.35*s2+.35*s3+.30*s4)
        return False,0
    def evaluate_new_pivot(self):
        vals=[]
        for t,fn,bonus in [("IMP",self.impulse,15),("ABC",self.abc,10),("FLAT",self.flat,10),("DEVC",self.devc,8),("DEV4",self.dev4,5),("DEV2",self.dev2,0)]:
            v,s=fn()
            if v and s>=MIN_SCORE: vals.append((s+bonus,s,t))
        if not vals:
            self.cType="NONE";self.cScore=0;self.cDir=0;self.inv=None;return
        vals.sort(reverse=True);_,raw,t=vals[0];self.cType=t;self.cScore=raw
        if t=="IMP":
            self.cDir=self.t(0);self.inv=None;self.impDir=self.cDir;self.impEndBar=self.bb(0);self.impEndPrice=self.p(0)
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
        if i>=2*PIV_LEN:
            j=i-PIV_LEN
            lo=j-PIV_LEN;hi=j+PIV_LEN
            hs=[self.b[k]["h"] for k in range(lo,hi+1)];ls=[self.b[k]["l"] for k in range(lo,hi+1)]
            ph=self.b[j]["h"] if self.b[j]["h"]==max(hs) else None
            pl=self.b[j]["l"] if self.b[j]["l"]==min(ls) else None
            minsw=(self.atr[i] or 0)*MIN_SWING_ATR
            if ph is not None and self.push(j,ph,1,minsw,i):new=True
            if pl is not None and self.push(j,pl,-1,minsw,i):new=True
        if new:self.evaluate_new_pivot()
        if self.cType in ("DEV2","DEV4","DEVC") and self.inv is not None:
            hit=(self.b[i]["l"]<self.inv) if self.cDir==1 else (self.b[i]["h"]>self.inv)
            if hit:self.cType="NONE";self.cScore=0;self.cDir=0;self.inv=None

def run(t):
    bars=get_bars(t["inst"])
    target_open=t["signal_close_ms"]-15*60*1000
    upto=[x for x in bars if x["ts"]<=target_open]
    if not upto or upto[-1]["ts"]!=target_open:
        raise RuntimeError(f'{t["inst"]}: target candle missing; last={upto[-1]["ts"] if upto else None}, n={len(bars)}')
    ew=EW(upto)
    for i in range(len(upto)):ew.step(i)
    need=1 if t["side"]=="LONG" else -1
    setup=ew.cType in ("DEV2","DEV4","DEVC")
    allowed=setup and ew.cDir==need and ew.cScore>=MIN_SCORE
    return {
        **t,
        "bars_loaded":len(bars),"bars_to_signal":len(upto),
        "signal_candle":upto[-1],
        "elliott_type":ew.cType,"elliott_dir":"LONG" if ew.cDir==1 else "SHORT" if ew.cDir==-1 else "NONE",
        "elliott_score":round(ew.cScore,4),"invalidation":ew.inv,
        "pivots_stored":len(ew.pT),"FILTER_ALLOWED":allowed,"LOSS_BLOCKED":not allowed
    }

for t in TESTS:
    try: print("RESULT "+json.dumps(run(t),ensure_ascii=False))
    except Exception as e: print("ERROR "+t["inst"]+" "+repr(e))
