"""Read-only daily video. No exchange API credentials or trading calls."""
import asyncio, json, math, os, re, subprocess, textwrap
from html import unescape
from urllib.request import Request, urlopen
from urllib.parse import quote
import numpy as np
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
    profile = isinstance(chart, dict)
    y=210
    for line in body:
        for text in textwrap.wrap(line,width=39 if profile else 62):
            d.text((58,y),text,font=font(24 if profile else 30),fill="#E8EEF5")
            y+=33 if profile else 44
        y+=18
    if profile:
        if chart['prices']:
            draw_market_chart(d, chart['prices'], font)
    elif chart and len(chart)>1:
        l,t,r,b=65,445,1215,600
        low=min(chart+[0]); high=max(chart+[0])
        if high==low: high=low+1
        yy=lambda v:b-(v-low)/(high-low)*(b-t)
        d.line((l,yy(0),r,yy(0)),fill="#596B80",width=2)
        pts=[(l+(r-l)*i/(len(chart)-1),yy(v)) for i,v in enumerate(chart)]
        d.line(pts,fill="#37D4B0",width=4)
        d.text((65,612),"Cumulative net P&L (USDT) after each closed trade",font=font(20),fill="#A7B6C6")
    d.text((52,674),"Historical records only  /  Not a forecast",font=font(20),fill="#A7B6C6")
    path=OUT/f"slide-{idx:02}.png"; im.save(path)
    return path
def market_closes(symbol):
    period = 300_000
    end = int(datetime.now(timezone.utc).timestamp() * 1000) // period * period
    url = ("https://openapi.blofin.com/api/v1/market/candles"
           f"?instId={quote(symbol)}-USDT&bar=5m&limit=290")
    try:
        with urlopen(Request(url, headers={"User-Agent": "BloFin-Daily-Report/1.0"}), timeout=8) as response:
            result = json.load(response)
        if str(result.get("code")) != "0":
            return []
        closes = {}
        for row in result.get("data") or []:
            if len(row) < 9 or str(row[8]) != "1":
                continue
            ts, close = int(row[0]) + period, float(row[4])
            if end - 86_400_000 < ts <= end and math.isfinite(close) and close > 0:
                closes[ts] = close
        return sorted(closes.items()) if len(closes) >= 2 else []
    except (OSError, ValueError, TypeError, IndexError) as error:
        print("BloFin candles unavailable:", symbol, error)
        return []


def draw_market_chart(d, prices, font):
    left, right, top, bottom = 642, 1200, 285, 550
    first, last = prices[0][0], prices[-1][0]
    low, high = min(p for _, p in prices), max(p for _, p in prices)
    d.text((left, 210), "BloFin futures  |  5-minute closes", font=font(26, True), fill="#F0F5FA")
    for y, value in [(top, high), (bottom, low)]:
        d.line((left, y, right, y), fill="#405368")
        d.text((left, y-26), f"{value:.7g} USDT", font=font(19), fill="#A7B6C6")
    previous = None
    for timestamp, price in prices:
        x = left + (timestamp-first)/(last-first)*(right-left)
        y = bottom - (price-low)/(high-low)*(bottom-top) if high > low else (top+bottom)/2
        if previous and timestamp - previous[0] == 300_000:
            d.line((previous[1], previous[2], x, y), fill="#37D4B0", width=3)
        else:
            d.ellipse((x-2, y-2, x+2, y+2), fill="#37D4B0")
        previous = timestamp, x, y
    for x, stamp, anchor in [(left, first, "la"), (right, last, "ra")]:
        label = datetime.fromtimestamp(stamp/1000, timezone.utc).strftime("%d %b %H:%M")
        d.text((x, bottom+12), label, anchor=anchor, font=font(19), fill="#A7B6C6")
    d.text((left, bottom+49), "Time in UTC", font=font(19), fill="#A7B6C6")


def coin_data(path):
    req = Request("https://api.coingecko.com/api/v3/" + path,
                  headers={"User-Agent": "BloFin-Daily-Report/1.0", "Accept": "application/json"})
    with urlopen(req, timeout=5) as response:
        return json.load(response, parse_float=Decimal)


# CoinGecko USD records checked on 26 November 2026. Slugs identify the
# source pages. Dated observations remain usable during public API throttling.
VERIFIED_PROFILES = {
  "CFG": [
    "Centrifuge",
    "centrifuge-2",
    "Centrifuge brings real-world assets such as credit funds onto blockchains as tokens.",
    "0.4004",
    "0.04401"
  ],
  "CLO": [
    "Clovis Network",
    "yei-finance",
    "Clovis, originally Yei Finance, connects lending, swaps and liquidity across blockchains.",
    "0.9101",
    "0.02523"
  ],
  "CP": [
    "Cluster Protocol",
    "cluster-protocol",
    "Cluster Protocol connects GPU providers with developers who need computing power for AI models.",
    "0.04629",
    "0.005121"
  ],
  "ME": [
    "Magic Eden",
    "magic-eden",
    "Magic Eden operates a marketplace for buying and selling digital collectibles across blockchains.",
    "17.00",
    "0.03313"
  ],
  "MOVE": [
    "Movement",
    "movement",
    "Movement is a blockchain network that lets developers build applications using the Move language.",
    "1.45",
    "0.004293"
  ],
  "MTL": [
    "Metal DAO",
    "metal",
    "Metal DAO uses MTL for governance, including decisions about the Metal Dollar stablecoin basket.",
    "17.03",
    "0.1173"
  ],
  "SPACE": [
    "Spacecoin",
    "spacecoin-2",
    "Spacecoin is developing a satellite network to provide decentralized internet access.",
    "0.02603",
    "0.002619"
  ],
  "TRX": [
    "TRON",
    "tron",
    "TRON is a blockchain for smart contracts and transfers of digital assets, including stablecoins.",
    "0.4313",
    "0.001804"
  ]
}

def verified_markets(symbols):
    slugs = [VERIFIED_PROFILES[s][1] for s in symbols if s in VERIFIED_PROFILES]
    if not slugs:
        return {}
    try:
        rows = coin_data("coins/markets?vs_currency=usd&ids=" + ",".join(slugs))
        return {(row["symbol"].upper(), row["id"]): row for row in rows}
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Using dated CoinGecko records:", error)
        return {}


def verified_profile(symbol, markets):
    name, slug, summary, high, low = VERIFIED_PROFILES[symbol]
    row = markets.get((symbol, slug), {})
    source = "CoinGecko | checked 26 Nov 2026"
    if row.get("ath") and row.get("atl") and row.get("last_updated"):
        high, low = row["ath"], row["atl"]
        source = "CoinGecko | " + row["last_updated"][:10]
    amount_high = format(Decimal(str(high)), "f")
    amount_low = format(Decimal(str(low)), "f")
    body = [summary, f"All-time high: USD {amount_high}", f"All-time low: USD {amount_low}", source]
    speech = (f"{summary} According to CoinGecko, as of {source.split('| ')[1].removeprefix('checked ')}, "
              f"its recorded high was {amount_high} US dollars and its low was {amount_low} US dollars.")
    print("Verified project:", symbol, name, amount_high, amount_low, source)
    return body, speech


def project_sections(instruments):
    symbols = set()
    for instrument in instruments:
        match = re.fullmatch(r"([A-Z0-9]+?)(?:[-_/]?USDT)(?:-SWAP)?", str(instrument).upper())
        if match:
            symbols.add(match[1])
    if not symbols:
        return
    known = {"DOGE": "dogecoin", "BTC": "bitcoin", "ETH": "ethereum",
             "SOL": "solana", "MERL": "merlin-chain"}
    candidates = {}
    markets = verified_markets(symbols)
    if symbols - known.keys() - VERIFIED_PROFILES.keys():
        try:
            for coin in coin_data("coins/list"):
                candidates.setdefault(coin["symbol"].upper(), []).append(coin["id"])
        except (OSError, ValueError, KeyError) as error:
            print("CoinGecko asset list unavailable:", error)
    for symbol in sorted(symbols):
        profile = verified_profile(symbol, markets) if symbol in VERIFIED_PROFILES else project_profile(symbol, known, candidates)
        body, speech = profile or (["Project identity or historical records unverified."], f"{symbol} futures on BloFin.")
        prices = market_closes(symbol)
        if prices and not profile:
            start, finish = prices[0][1], prices[-1][1]
            change = 100 * (finish / start - 1)
            body.append(f"First / last close: {start:.7g} / {finish:.7g} USDT")
            body.append(f"Change across plotted closes: {change:+.2f}%")
            speech += f" Across the chart, the futures price changed by {change:.2f} percent."
        if not prices:
            body.append("5-minute price history unavailable")
        yield (symbol + " | Project profile", body, speech, {"prices": prices})


def project_profile(symbol, known, candidates):
    ids = [known[symbol]] if symbol in known else candidates.get(symbol, [])
    if len(ids) != 1:
        print("No unique project match:", symbol)
        return
    try:
        coin = coin_data("coins/" + quote(ids[0], safe="") +
                        "?localization=false&tickers=false&community_data=false&developer_data=false&sparkline=false")
        if coin["id"] != ids[0] or coin["symbol"].upper() != symbol:
            return
        name = coin["name"]
        raw = (coin.get("description") or {}).get("en") or ""
        plain = " ".join(unescape(re.sub(r"<[^>]*>", " ", raw)).split())
        summary = next((s for s in re.split(r"(?<=[.!?])\s+", plain)
                        if 20 <= len(s) <= 200 and "http" not in s), "")
        if symbol == "DOGE":
            summary = "Dogecoin is a peer-to-peer digital currency used for payments and online tipping."
        body = [summary] if summary else []
        speech = [name + ".", summary] if summary else [name + "."]
        market = coin.get("market_data") or {}
        for field, label in (("ath", "All-time high"), ("atl", "All-time low")):
            price = (market.get(field) or {}).get("usd")
            date = (market.get(field + "_date") or {}).get("usd")
            if price is None or not date:
                continue
            value = Decimal(str(price))
            if not value.is_finite() or value <= 0:
                continue
            amount = format(value.normalize(), "f")
            when = datetime.fromisoformat(date.replace("Z", "+00:00")).strftime("%d %b %Y")
            body.append(f"{label}: USD {amount} ({when})")
            speech.append(f"Its recorded {label.lower()} was {amount} US dollars on {when}.")
        if body:
            return body + ["Source: CoinGecko"], " ".join(speech)
    except (OSError, ValueError, KeyError, ArithmeticError) as error:
        print("CoinGecko project unavailable:", symbol, error)


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
    traded = [row.get("inst") or row.get("instId") or row.get("symbol") for _, _, row in daily]
    sections.extend(project_sections(traded + opened))
    chart=[0.0]; running=Decimal(0)
    for _,p,_ in sample:
        running+=p; chart.append(float(running))
    sections.append(("Bot performance after fees",[f"Since {START:%d/%m/%Y}",f"Net P&L: {ptotal:+.4f} USDT | Win rate: {rate:.1f}%",f"{wins} wins, {losses} losses, {neutral} flat"],"Since the twenty eighth of October, the recorded R S I short sample contains "+words(len(sample))+" closed trades. "+words(wins)+" were profitable, "+words(losses)+" lost money, and "+words(neutral)+" broke even. The win rate is "+ratewords+". "+pnl_text(ptotal)+". The chart shows the cumulative recorded net result for this sample. Earlier strategies are not included.",chart))
    sections.append(("Risk and next update",["A high win rate does not guarantee profits.","Results can vary. Capital is at risk."],"A high win rate does not guarantee profits. Losing trades and trading fees matter. This journal reports historical results for the specified strategy, not a recommendation to trade. Thank you for watching.",None))
    kokoro=Kokoro("kokoro-v1.0.onnx","voices-v1.0.bin")
    segments = []
    voice_path = OUT/"narration.wav"
    sample_rate = 24000
    frames_per_second = 24
    samples_per_frame = sample_rate // frames_per_second
    with soundfile.SoundFile(voice_path, mode="w", samplerate=sample_rate, channels=1, subtype="FLOAT") as track:
        for i, (title, body, narration, chart) in enumerate(sections):
            image = slide(title, body, i, chart)
            start = track.tell()
            for sentence in re.split(r"(?<=[.!?])\s+", narration):
                samples, rate = kokoro.create(sentence, voice="bm_george", speed=1.0, lang="en-gb", trim=False)
                if rate != sample_rate or not len(samples) or not np.isfinite(samples).all():
                    raise ValueError("Invalid synthesized sentence audio")
                track.write(samples)
            frame_count = math.ceil((track.tell() - start) / samples_per_frame)
            track.write(np.zeros(frame_count * samples_per_frame - (track.tell() - start), dtype=np.float32))
            segment = OUT/f"seg-{i}.mp4"
            subprocess.run([FF, "-y", "-v", "error", "-loop", "1", "-framerate", str(frames_per_second),
                            "-i", str(image), "-frames:v", str(frame_count), "-c:v", "libx264",
                            "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p", str(segment)], check=True)
            segments.append(segment)
        total_samples = track.tell()
    (OUT/"concat.txt").write_text("".join("file '"+s.name+"'\n" for s in segments))
    inputs = [FF, "-hide_banner", "-nostats", "-f", "concat", "-safe", "0", "-i", str(OUT/"concat.txt"), "-i", str(voice_path)]
    target = "loudnorm=I=-13:LRA=7:TP=-1.5"

    def measure(source):
        result = subprocess.run(source + ["-vn", "-af", target + ":print_format=json", "-f", "null", "-"],
                                check=True, capture_output=True, text=True)
        return json.loads(result.stderr[result.stderr.rindex("{"):result.stderr.rindex("}")+1])

    before = measure(inputs)
    stats = ":".join(f"measured_{key}={before['input_' + key]}" for key in ("i", "lra", "tp", "thresh"))
    audio_filter = f"{target}:{stats}:offset={before['target_offset']}:linear=true"
    report_path = OUT/"report.mp4"
    subprocess.run(inputs + ["-y", "-v", "error", "-map", "0:v:0", "-map", "1:a:0",
                             "-c:v", "copy", "-af", audio_filter, "-c:a", "aac", "-b:a", "192k",
                             "-ar", "48000", "-movflags", "+faststart", str(report_path)], check=True)
    decoded = subprocess.check_output([FF, "-v", "error", "-i", str(report_path), "-vn",
                                       "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"])
    final_samples = np.frombuffer(decoded, dtype="<f4")
    if abs(len(final_samples) - total_samples) > sample_rate * 0.05:
        raise RuntimeError("Final audio length differs from the continuous narration")
    original, _ = soundfile.read(voice_path, dtype="float32")
    # Detect lost speech within a 20 ms window after the final encode.
    window = sample_rate // 50
    usable = min(len(original), len(final_samples)) // window * window
    def rms(signal):
        return np.sqrt(np.mean(signal[:usable].reshape(-1, window)**2, axis=1))
    if np.any((rms(original) > 0.01) & (rms(final_samples) < 0.0001)):
        raise RuntimeError("Final audio contains a dropout during voiced speech")
    print(f"Continuous narration checked: {total_samples / sample_rate:.2f} seconds, 0 missing speech windows")
    after = measure([FF, "-hide_banner", "-nostats", "-i", str(report_path)])
    print(f"Loudness before {before['input_i']} LUFS, final {after['input_i']} LUFS, peak {after['input_tp']} dBTP")
    if not -14.5 <= float(after["input_i"]) <= -11.5 or not float(after["input_tp"]) < 0:
        raise RuntimeError("Final narration loudness or peak outside target")
    print("Rendered verified trades:",len(sample),"report bytes:",(OUT/"report.mp4").stat().st_size)
    if os.environ.get("REPORT_EDITION"):
        date += " | " + os.environ["REPORT_EDITION"]
    (OUT/"metadata.json").write_text(json.dumps({"title":"BloFin RSI Short Bot | Daily Trading Report | "+date,"description":"Historical results for RSI89_SHORT since 28 October 2026 only; excludes earlier strategies. Last 24-hour results are realized net of recorded fees. Not financial or any other advice. Capital is at risk.\nSource: https://github.com/Photofiver/Stock-finder","tags":["BloFin","RSI","trading bot","crypto trading","trading journal"]},indent=2))
if __name__=="__main__":
    asyncio.run(render())
