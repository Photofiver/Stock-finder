"""Read-only daily video. No exchange API credentials or trading calls."""
import asyncio, json, math, os, subprocess, textwrap
from pathlib import Path
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from num2words import num2words
import imageio_ffmpeg, soundfile
from kokoro_onnx import Kokoro

OUT=Path("youtube_output")
OUT.mkdir(exist_ok=True)
FF=imageio_ffmpeg.get_ffmpeg_exe()
UK=ZoneInfo("Europe/London")
START=datetime(2026,10,28,tzinfo=UK)
def words(n, **kwargs):
    return num2words(n,lang="en_GB",**kwargs)
def money(n):
    n=Decimal(str(n))
    if not n.is_finite(): raise ValueError("Invalid amount")
    sign="minus " if n<0 else ""
    n=abs(n)
    if n==0: return "zero Tether"
    if n<Decimal("0.005"): return sign+"less than one cent in Tether"
    cents=int((n*100).quantize(Decimal("1")))
    if cents<100: return sign+words(cents)+" cents in Tether"
    whole,frac=divmod(cents,100)
    return sign+words(whole)+(" dollar" if whole==1 else " dollars")+((" and "+words(frac)+" cents") if frac else "")+" in Tether"
def pnl_text(n):
    return ("Net loss: "+money(abs(n))) if n<0 else ("Net profit: "+money(n))
def slide(title,body,idx,chart=None):
    im=Image.new("RGB",(1280,720),"#0D1825")
    d=ImageDraw.Draw(im)
    def font(size,bold=False):
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans"+("-Bold" if bold else "")+".ttf",size)
    d.rectangle((0,0,1280,6),fill="#37D4B0")
    d.text((52,30),"LIVE BOT JOURNAL  /  BLOFIN",font=font(24,True),fill="#37D4B0")
    d.text((52,97),title,font=font(49,True),fill="#F0F5FA")
    d.line((52,172,1228,172),fill="#405368",width=2)
    y=210
    for line in body:
        for text in textwrap.wrap(line,width=62):
            d.text((58,y),text,font=font(30),fill="#E8EEF5")
            y+=44
        y+=18
    if chart and len(chart)>1:
        l,t,r,b=65,445,1215,600
        low=min(chart+[0]); high=max(chart+[0])
        if high==low: high=low+1
        yy=lambda v:b-(v-low)/(high-low)*(b-t)
        d.line((l,yy(0),r,yy(0)),fill="#596B80",width=2)
        pts=[(l+(r-l)*i/(len(chart)-1),yy(v)) for i,v in enumerate(chart)]
        d.line(pts,fill="#37D4B0",width=4)
        d.text((65,612),"Cumulative net P&L since the start of this sample",font=font(20),fill="#A7B6C6")
    d.text((52,674),"Historical records only  /  Not a forecast",font=font(20),fill="#A7B6C6")
    path=OUT/f"slide-{idx:02}.png"; im.save(path)
    return path
async def render():
    data=json.loads(Path(os.environ.get("STATE_PATH","blofin_live_state.json")).read_text())
    now=datetime.now(timezone.utc)
    last=int(data.get("last_run_ms") or 0)
    if not last or not -300<now.timestamp()-last/1000<1800: raise RuntimeError("State is stale; refusing to publish")
    rows=[]
    ids=set()
    for row in data.get("trade_history",[]):
        if not isinstance(row,dict): continue
        if "net_pnl_usdt" not in row or not row.get("closed_ms"): continue
        t=int(row["closed_ms"]); p=Decimal(str(row["net_pnl_usdt"]))
        if not p.is_finite() or t>now.timestamp()*1000: raise ValueError("Invalid trade")
        hid=row.get("history_id")
        if hid and hid in ids: raise ValueError("Duplicate trade")
        if hid: ids.add(hid)
        rows.append((t,p,row))
    rows.sort(key=lambda x:x[0])
    sample=[x for x in rows if x[0]>=START.timestamp()*1000 and (x[2].get("signal_snapshot") or {}).get("rsi89_short_entry") is True and str(x[2].get("side","")).upper()=="SHORT"]
    if not sample: raise RuntimeError("No verified RSI89_SHORT sample; refusing to invent statistics")
    daily=[x for x in sample if x[0]>=(now-timedelta(hours=24)).timestamp()*1000]
    start=Decimal(str(data["bankroll_start_usdt"]))
    bonus=Decimal(str(data.get("bankroll_streak_bonus_usdt",0)))
    net=Decimal(str(data["realized_pnl_usdt"]))-Decimal(str(data["bankroll_pnl_baseline_usdt"]))
    operational=max(Decimal(0),min(Decimal(70),start+bonus+net))
    p24=sum((x[1] for x in daily),Decimal(0))
    ptotal=sum((x[1] for x in sample),Decimal(0))
    wins=sum(x[1]>0 for x in sample); losses=sum(x[1]<0 for x in sample)
    neutral=len(sample)-wins-losses
    rate=100*wins/len(sample)
    ratewords=words(round(rate,1))+" percent"
    pos=data.get("positions") or {}
    if not isinstance(pos,dict): raise ValueError("Unexpected position format")
    opened=[str(p.get("inst",k)) for k,p in pos.items() if isinstance(p,dict)]
    date=now.astimezone(UK).strftime("%d %B %Y")
    day=now.astimezone(UK).day
    datewords=words(day,to="ordinal")+" of "+now.astimezone(UK).strftime("%B")+", "+words(now.astimezone(UK).year)
    sections=[
      ("Important notice",["This is not financial advice or any other advice.","Historical trading results are not a prediction."],"This is not financial advice, trading advice, or any other advice. This video is for informational purposes only. Past performance does not guarantee future results.",None),
      ("Daily trading report",[f"Reporting date: {now.astimezone(UK):%d/%m/%Y}","Live RSI short strategy. Actual recorded trades."],"Welcome to the daily crypto trading bot report for the "+datewords+". We review actual recorded results for the current R S I short strategy on BloFin, including the last twenty four hours.",None),
      ("Trading capital",[f"Current operating capital: {operational:.2f} USDT","10 consecutive wins: add up to 10 USDT","Current capital limit: 70 USDT"],"My current operating capital is "+money(operational)+". After each streak of ten consecutive winning trades, the bot adds up to ten Tether to the trading capital, subject to the current limit of seventy Tether.",None),
      ("Last 24 hours",[f"Net P&L: {p24:+.4f} USDT",f"{len(daily)} closed trades",f"{sum(x[1]>0 for x in daily)} wins, {sum(x[1]<0 for x in daily)} losses"],"The current strategy recorded "+words(len(daily))+" closed trades in the last twenty four hours. "+pnl_text(p24)+". These results include the recorded trading fees.",None),
      ("Open positions",[f"{len(opened)} open positions","; ".join(opened) if opened else "No open positions","Unrealized results are excluded from realized profit."],"There are currently "+words(len(opened))+" open positions across the bot. Unrealized results are not included in the realized profit. Their outcomes are not yet known.",None),
    ]
    chart=[0.0]; running=Decimal(0)
    for _,p,_ in sample:
        running+=p; chart.append(float(running))
    sections.append(("RSI short strategy record",[f"Since {START:%d/%m/%Y}",f"Net P&L: {ptotal:+.4f} USDT | Win rate: {rate:.1f}%",f"{wins} wins, {losses} losses, {neutral} flat"],"Since the twenty eighth of October, the recorded R S I short sample contains "+words(len(sample))+" closed trades. "+words(wins)+" were profitable, "+words(losses)+" lost money, and "+words(neutral)+" broke even. The win rate is "+ratewords+". "+pnl_text(ptotal)+". The chart shows the cumulative recorded net result for this sample. Earlier strategies are not included.",chart))
    sections.append(("Risk and next update",["A high win rate does not guarantee profits.","Results can vary. Capital is at risk."],"A high win rate does not guarantee profits. Losing trades and trading fees matter. This journal reports historical results for the specified strategy, not a recommendation to trade. Thank you for watching.",None))
    kokoro=Kokoro("kokoro-v1.0.onnx","voices-v1.0.bin")
    segments=[]
    for i,(title,body,narration,chart) in enumerate(sections):
        image=slide(title,body,i,chart)
        voice=OUT/f"voice-{i}.wav"
        samples, sr=kokoro.create(narration,voice="bm_george",speed=1.0,lang="en-gb")
        soundfile.write(str(voice),samples,sr)
        segment=OUT/f"seg-{i}.mp4"
        # Audio starts without an artificial intro delay. Use the same settings throughout.
        subprocess.run([FF,"-y","-v","error","-loop","1","-framerate","24","-i",str(image),"-i",str(voice),"-c:v","libx264","-preset","veryfast","-tune","stillimage","-pix_fmt","yuv420p","-c:a","aac","-ar","48000","-ac","2","-shortest",str(segment)],check=True)
        segments.append(segment)
    (OUT/"concat.txt").write_text("".join("file '"+s.name+"'\n" for s in segments))
    inputs = [FF, "-hide_banner", "-nostats", "-f", "concat", "-safe", "0", "-i", str(OUT/"concat.txt")]
    target = "loudnorm=I=-13:LRA=7:TP=-1.5"

    def measure(source):
        result = subprocess.run(source + ["-vn", "-af", target + ":print_format=json", "-f", "null", "-"],
                                check=True, capture_output=True, text=True)
        return json.loads(result.stderr[result.stderr.rindex("{"):result.stderr.rindex("}")+1])

    before = measure(inputs)
    stats = ":".join(f"measured_{key}={before['input_' + key]}" for key in ("i", "lra", "tp", "thresh"))
    audio_filter = f"{target}:{stats}:offset={before['target_offset']}:linear=true"
    report_path = OUT/"report.mp4"
    subprocess.run(inputs + ["-y", "-v", "error", "-map", "0:v:0", "-map", "0:a:0",
                             "-c:v", "copy", "-af", audio_filter, "-c:a", "aac", "-b:a", "192k",
                             "-ar", "48000", "-movflags", "+faststart", str(report_path)], check=True)
    after = measure([FF, "-hide_banner", "-nostats", "-i", str(report_path)])
    print(f"Loudness before {before['input_i']} LUFS, final {after['input_i']} LUFS, peak {after['input_tp']} dBTP")
    if not -14.5 <= float(after["input_i"]) <= -11.5 or not float(after["input_tp"]) < 0:
        raise RuntimeError("Final narration loudness or peak outside target")
    print("Rendered verified trades:",len(sample),"report bytes:",(OUT/"report.mp4").stat().st_size)
    (OUT/"metadata.json").write_text(json.dumps({"title":"BloFin RSI Short Bot | Daily Trading Report | "+date,"description":"Historical results for RSI89_SHORT since 28 October 2026 only; excludes earlier strategies. Last 24-hour results are realized net of recorded fees. Not financial or any other advice. Capital is at risk.\nSource: https://github.com/Photofiver/Stock-finder","tags":["BloFin","RSI","trading bot","crypto trading","trading journal"]},indent=2))
if __name__=="__main__":
    asyncio.run(render())
