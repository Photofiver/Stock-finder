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
    title_size=49
    while title_size>24 and d.textlength(title,font=font(title_size,True))>1176: title_size-=2
    d.text((52,97),title,font=font(title_size,True),fill="#F0F5FA")
    d.line((52,172,1228,172),fill="#405368",width=2)
    profile = isinstance(chart, dict)
    replay = profile and chart.get("kind") == "replay"
    y=210
    for line in body:
        for text in textwrap.wrap(line,width=36 if replay else 39 if profile else 62):
            d.text((58,y),text,font=font(24 if profile else 30),fill="#E8EEF5")
            y+=33 if profile else 44
        y+=18
    if profile:
        if replay:
            draw_trade_replay(d, chart, font)
        elif chart.get("prices"):
            draw_market_chart(d, chart["prices"], font)
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
    path=OUT/f"slide-{idx}.png"; im.save(path)
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
        if prices:
            start, finish = prices[0][1], prices[-1][1]
            change = 100 * (finish / start - 1)
            if not profile:
                body.append(f"First / last close: {start:.7g} / {finish:.7g} USDT")
            direction = "rose" if change > 0 else "fell" if change < 0 else "was unchanged"
            speech += (f" On BloFin, the price {direction}" + (f" by {abs(change):.2f} percent" if change else "") + " across the available five minute closes. This is the market move, not the bot's trade return.")
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


def finite_number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None

def trade_candles(row):
    candles = []
    for bar in (row.get("volume_candle_audit") or {}).get("candles", []):
        try:
            t = int(bar["close_ms"])
            o,h,l,c = [float(bar[k]) for k in ("open","high","low","close")]
            if all(math.isfinite(x) and x > 0 for x in (o,h,l,c)) and l <= min(o,c) <= max(o,c) <= h and bar.get("price_exposure") == "FULL" and t <= int(row["closed_ms"]):
                candles.append((t,o,h,l,c))
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return sorted({x[0]: x for x in candles}.values())

def draw_trade_replay(d, chart, font):
    row = chart["row"]
    candles = chart["candles"]
    fraction = chart.get("fraction", 1)
    left,right,top,bottom = 645,1200,260,550
    opened,closed = int(row["opened_ms"]),int(row["closed_ms"])
    entry,exit_price = float(row["open_price"]),float(row["close_price"])
    values = [entry, exit_price] + [v for bar in candles for v in bar[1:]]
    lo,hi = min(values),max(values)
    pad=max((hi-lo)*0.09, hi*0.0001)
    lo,hi=lo-pad,hi+pad
    xx=lambda t:left+(right-left)*(t-opened)/max(1,closed-opened)
    yy=lambda p:bottom-(p-lo)/(hi-lo)*(bottom-top)
    d.text((left,210),"Trade replay | 5m candles",font=font(25,True),fill="#F0F5FA")
    for value in (lo,(lo+hi)/2,hi):
        y=yy(value)
        d.line((left,y,right,y),fill="#405368")
        d.text((left,y-23),f"{value:.7g}",font=font(17),fill="#A7B6C6")
    limit=opened+(closed-opened)*fraction
    width=max(1,min(8,(right-left)*300000/max(1,closed-opened)*0.32))
    for t,o,h,l,c in candles:
        if t>limit: continue
        x=xx(t-150000)
        if not left<=x<=right: continue
        color="#37D4B0" if c>=o else "#F36B7D"
        d.line((x,yy(h),x,yy(l)),fill=color,width=2)
        y1,y2=sorted((yy(o),yy(c)))
        d.rectangle((max(left,x-width),y1,min(right,x+width),max(y1+1,y2)),fill=color)
    d.line((left,yy(entry),xx(limit),yy(entry)),fill="#EFC66B",width=2)
    d.ellipse((left-5,yy(entry)-5,left+5,yy(entry)+5),fill="#EFC66B")
    d.text((left,570),"ENTRY "+datetime.fromtimestamp(opened/1000,UK).strftime("%H:%M"),font=font(19),fill="#EFC66B")
    if fraction>=1 and chart.get("reveal", True):
        d.ellipse((right-6,yy(exit_price)-6,right+6,yy(exit_price)+6),fill="#F0F5FA")
        d.text((right,570),"EXIT "+datetime.fromtimestamp(closed/1000,UK).strftime("%H:%M"),anchor="ra",font=font(19),fill="#F0F5FA")
    else:
        d.text((right,570),"REPLAY IN PROGRESS",anchor="ra",font=font(19),fill="#A7B6C6")
    d.text((left,612),"Full candles only | gaps not estimated",font=font(18),fill="#A7B6C6")

def replay_sections(item,label):
    _,p,row=item
    symbol=str(row.get("inst") or row.get("symbol") or "Instrument")
    snap=row.get("signal_snapshot") or {}
    rsi=finite_number(snap.get("rsi14"))
    prev=finite_number(snap.get("rsi14_previous"))
    entry=finite_number(row.get("open_price"))
    exit_price=finite_number(row.get("close_price"))
    minutes=finite_number(row.get("hold_minutes"))
    peak=row.get("peak_position_before_exit") or {}
    best=finite_number(peak.get("peak_profit_pct_before_exit")) if peak.get("available") else None
    review=row.get("post_trade_review") or {}
    actual=finite_number(review.get("actual_directional_price_pct"))
    if actual is None and entry and exit_price:
        actual=(entry-exit_price)/entry*100
    result="a loss" if p<0 else "a win" if p>0 else "flat"
    body=["SHORT | recorded entry signal"]
    speech=f"Here is the {label.lower()} trade, {symbol}. The bot entered short."
    if rsi is not None and prev is not None:
        body.append(f"RSI: {prev:.2f} to {rsi:.2f}")
        speech+=f" The five minute R S I moved from {prev:.2f} to {rsi:.2f}. A high R S I does not guarantee a reversal."
    if entry and exit_price:
        body.append(f"Entry {entry:.7g} | exit {exit_price:.7g}")
    if minutes is not None:
        body.append(f"Duration: {minutes:.1f} minutes")
        speech+=f" The position remained open for {minutes:.1f} minutes."
    if best is not None:
        body.append(f"Best observed gross: {best:+.2f}%")
        speech+=f" The most favourable observed gross price return was {best:.2f} percent, before fees. This is a historical observation, not an executable alternative."
    if actual is not None:
        body.append(f"Final price return: {actual:+.2f}%")
        speech+=f" At the recorded exit the directional price return was {actual:.2f} percent."
    body.append(f"Net result: {p:+.4f} USDT")
    speech+=f" The recorded outcome was {result}. {pnl_text(p)}. "
    if review.get("had_gross_profit_before_net_loss") is True:
        speech+="The trade had an earlier favourable excursion, then finished negative. That is a reason to investigate exits, not proof that a different stop would work. "
    speech+="The exact closing trigger is not assumed when the history only records an external close."
    candles=trade_candles(row)
    chart={"kind":"replay","row":row,"candles":candles} if candles and entry and exit_price and row.get("opened_ms") and row.get("closed_ms") else None
    if chart is None:
        body.append("Complete replay data unavailable")
    return (f"{symbol} | {label}",body,speech,chart)

def threshold_section(sample):
    groups=[]
    for threshold in (89,92,93):
        subset=[x for x in sample if (finite_number((x[2].get("signal_snapshot") or {}).get("rsi14")) or 0)>=threshold]
        if subset:
            w=sum(x[1]>0 for x in subset)
            groups.append((threshold,len(subset),100*w/len(subset)))
    body=[f"RSI >= {t}: {n} trades | {w:.1f}% wins" for t,n,w in groups]
    speech="Would a stricter entry threshold have selected a different group of past trades? "
    speech+=" ".join(f"At R S I {t} or higher, {n} recorded trades qualified, with a win rate of {w:.1f} percent." for t,n,w in groups)
    speech+=" This is a subset of actual entries, not a simulated strategy or a prediction. It does not establish how missed or alternative entries would behave."
    return ("RSI threshold experiment",body+["Recorded subsets only; not a backtest"],speech,None)

def headline_for(daily):
    if not daily:
        return "No New Closes: What Is the Bot Watching?"
    twists=[x for x in daily if (x[2].get("post_trade_review") or {}).get("had_gross_profit_before_net_loss") is True]
    if twists:
        symbol=str(min(twists,key=lambda x:x[1])[2].get("inst") or "Trade")
        return f"{symbol}: A Winning Moment, Then a Loss"
    best=max(daily,key=lambda x:x[1])
    worst=min(daily,key=lambda x:x[1])
    if best[1]>0 and worst[1]<0:
        return f"{best[2].get('inst','Trade')} vs {worst[2].get('inst','Trade')}: Same Signal, Different Ends"
    if worst[1]<0:
        return f"{worst[2].get('inst','Trade')}: When the RSI Signal Failed"
    return f"{best[2].get('inst','Trade')}: How the RSI Signal Played Out"

def make_thumbnail(headline,date):
    im=Image.new("RGB",(1280,720),"#0D1825")
    d=ImageDraw.Draw(im)
    def font(size):
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",size)
    d.rectangle((0,0,1280,16),fill="#37D4B0")
    d.text((55,56),"LIVE BOT EXPERIMENT",font=font(43),fill="#37D4B0")
    y=175
    for line in textwrap.wrap(headline,width=22):
        d.text((55,y),line,font=font(64),fill="#F0F5FA")
        y+=78
    d.text((55,650),date+" | BLOFIN",font=font(29),fill="#A7B6C6")
    im.save(OUT/"thumbnail.png")


def story_sections(daily, sample, opened, prior, weekly, date):
    """One evidence-led story; no account balances, cash P&L or invented events."""
    def symbol(row):
        return str(row.get("inst") or row.get("instId") or row.get("symbol") or "Market").split("-")[0]
    def pct(x):
        return f"{x:.1f} percent"
    def chart_for(row, fraction, reveal=False):
        bars=trade_candles(row)
        entry=finite_number(row.get("open_price"))
        close=finite_number(row.get("close_price"))
        if not bars or not entry or not close or not row.get("opened_ms") or not row.get("closed_ms"):
            return None
        return {"kind":"replay","row":row,"candles":bars,"fraction":fraction,
                "max_fraction":fraction,"reveal":reveal}
    beats=[]
    def add(title, body, speech, chart=None):
        beats.append((title,body,speech,chart))
    if daily:
        # Prefer actual turning points with verified replay data, then directional movement.
        def rank(item):
            row=item[2]
            review=row.get("post_trade_review") or {}
            return (bool(trade_candles(row)), bool(review.get("had_gross_profit_before_net_loss") and item[1]<0),
                    abs(finite_number(review.get("actual_directional_price_pct")) or 0),
                    finite_number((row.get("signal_snapshot") or {}).get("rsi14")) or 0)
        focus=max(daily,key=rank)
        _,p,row=focus
        sym=symbol(row)
        snap=row.get("signal_snapshot") or {}
        rsi=finite_number(snap.get("rsi14"))
        previous=finite_number(snap.get("rsi14_previous"))
        entry=finite_number(row.get("open_price"))
        exit_price=finite_number(row.get("close_price"))
        duration=finite_number(row.get("hold_minutes"))
        review=row.get("post_trade_review") or {}
        peak=row.get("peak_position_before_exit") or {}
        best=finite_number(peak.get("peak_profit_pct_before_exit")) if peak.get("available") else None
        actual=finite_number(review.get("actual_directional_price_pct"))
        if actual is None and entry and exit_price:
            actual=(entry-exit_price)/entry*100
        title=f"{sym}: Can RSI Predict a Reversal?"
        thumb="REVERSAL OR TRAP?"
        add("Reversal or trap?",[sym,"RSI experiment"],f"Can {sym} reverse after this signal? Watch the price. We will follow one real trade from entry to exit.",chart_for(row,0))
        add("The rule",["RSI 89","Five-minute candles"],"The bot watches a measure called R S I. It measures the strength of recent price moves. The short signal appears when R S I crosses eighty-nine. A short position expects the price to fall. But a strong move can keep going.",chart_for(row,0))
        if rsi is not None and previous is not None:
            add("The signal",[f"RSI {previous:.2f} to {rsi:.2f}","Recorded entry"],f"Look at the signal. R S I moved from {previous:.2f} to {rsi:.2f}. The bot recorded a short entry. This tells us why the setup qualified. It does not tell us what happens next.",chart_for(row,0))
        else:
            add("The signal",["Recorded short entry","Exact RSI unavailable"],"The trade qualified for the short strategy. The exact indicator readings are missing from this record. We will not guess them.",chart_for(row,0))
        if entry:
            add("At entry",[f"Entry {entry:.7g}","Direction: down"],"The marked line shows the entry level. From here, lower prices favour the short position. Higher prices work against it. Keep that line in mind as the candles appear.",chart_for(row,0))
        bars=trade_candles(row)
        valid_chart=chart_for(row,0)
        if valid_chart:
            opened_ms=int(row["opened_ms"]); closed_ms=int(row["closed_ms"])
            prior_count=0
            for stage,fraction in enumerate((.25,.50,.75,1.0),1):
                limit=opened_ms+(closed_ms-opened_ms)*fraction
                visible=[b for b in bars if opened_ms<=b[0]<=limit]
                new_count=len(visible)-prior_count
                prior_count=len(visible)
                label=f"Replay {stage} of 4"
                if visible and new_count and entry:
                    change=(visible[-1][4]-entry)/entry*100
                    direction="above" if change>0 else "below" if change<0 else "at"
                    phrase=f"The latest full candle closes {direction} the entry level."
                    distance=f" The distance is {abs(change):.2f} percent." if change else ""
                    speech=f"{phrase}{distance} This is a recorded candle close, not the final trade result."
                    if stage==1: speech+=" Watch whether the price moves back across the entry line."
                    elif stage==2: speech+=" Notice the difference between a candle close and its highest point."
                    elif stage==3: speech+=" The signal alone cannot explain every move along this path."
                    else: speech+=" Now compare the last full candle with the actual exit."
                    body=[f"{direction.capitalize()} entry",f"{len(visible)} full candles"]
                else:
                    speech="No additional full candle is recorded in this interval. The missing price movement cannot be reconstructed. We keep the gap visible instead of drawing an invented path."
                    body=["No new full candles","Gap not estimated"]
                add(label,body,speech,chart_for(row,fraction))
        else:
            add("What is missing",["Full replay unavailable"],"The full candle history is missing for this trade. A smooth animation would hide that gap. We can still compare the recorded entry and exit. We cannot claim to know the entire price path.")
        if best is not None:
            add("Best observed move",[f"{best:+.2f}% gross","Before recorded exit"],f"The most favourable recorded move was {best:.2f} percent before fees. This is a historical observation. It does not prove that the bot could exit at that point.",chart_for(row,1))
        if duration is not None:
            add("Time in the trade",[f"{duration:.1f} minutes"],f"The position stayed open for {duration:.1f} minutes. The timing matters because the market can change after an entry. We only use the recorded duration.",chart_for(row,1))
        outcome="Win" if p>0 else "Loss" if p<0 else "Break-even"
        result_body=[outcome+" after fees"]
        if actual is not None: result_body.append(f"Price move {actual:+.2f}%")
        speech=f"Here is the recorded outcome. It was a {outcome.lower()}. "
        if actual is not None: speech+=f"The directional price return was {actual:.2f} percent. "
        if review.get("had_gross_profit_before_net_loss") and p<0:
            speech+="The position had an earlier favourable move. Yet it closed negative after fees. This is why an entry signal is not the whole story."
        else: speech+="The outcome reflects both the entry and the exit. A single trade cannot establish whether the rule works."
        add("The result",result_body,speech,chart_for(row,1,True))
        # Only the featured project, no unrelated coin catalogue.
        instrument=str(row.get("inst") or row.get("instId") or row.get("symbol") or "")
        for _,body,_,chart in project_sections([instrument]):
            summary=next((b for b in body if not b.startswith(("All-time","Source:","CoinGecko","First /","5-minute","Project identity"))),"")
            if summary:
                sentences=[v.strip() for v in re.split(r"(?<=[.!?])\s+",summary) if v.strip()]
                # Keep long sourced text out of the short narration; display it as context.
                spoken=" ".join(v for v in sentences if len(v.split())<=20)
                add("What is "+sym+"?",[summary],spoken or f"This is the recorded project description for {sym}.",chart)
            records=[b for b in body if b.startswith(("All-time high","All-time low"))]
            if records:
                add("Historical price extremes",records+["Source: CoinGecko"],"These are the recorded historical price extremes. They provide market context, not a prediction.",chart)
            if chart and chart.get("prices"):
                add("Market context",[sym,"Five-minute closes"],"This chart shows the market beyond the single trade. The wider price movement is different from the bot's result.",chart)
    else:
        title="Why Did the Bot Stay Quiet?"
        thumb="NO NEW CLOSES"
        add("Why so quiet?",["No qualifying closes"],"Why did the bot stay quiet? No qualifying trade closed in the last twenty-four hours. That does not mean the market stopped moving.")
        add("The rule",["RSI 89","Five-minute candles"],"The bot waits for its short entry signal. R S I must cross eighty-nine. Without verified entry data, we cannot claim why individual markets were skipped. Waiting is part of a rule-based approach.")
        add("Still open",[f"{len(opened)} positions"],f"There are {len(opened)} open positions. Their results are not known yet. An open position is not a completed trade.")
    def wins(items): return sum(x[1]>0 for x in items)
    def losses(items): return sum(x[1]<0 for x in items)
    add("Last 24 hours",[f"{len(daily)} closed",f"{wins(daily)} wins / {losses(daily)} losses"],f"Now put this story in context. The last twenty-four hours had {len(daily)} closed trades. {wins(daily)} were wins and {losses(daily)} were losses. The rest, if any, broke even.")
    add("Compared with yesterday",[f"Previous: {len(prior)} closed",f"Latest: {len(daily)} closed"],f"The previous twenty-four-hour window had {len(prior)} closed trades. The latest had {len(daily)}. These counts measure activity, not the quality of each signal.")
    for threshold in (89,92,93):
        subset=[x for x in sample if (finite_number((x[2].get("signal_snapshot") or {}).get("rsi14")) or 0)>=threshold]
        if subset:
            rate=100*wins(subset)/len(subset)
            add(f"RSI {threshold}",[f"{len(subset)} recorded trades",f"{rate:.1f}% wins"],f"What if we look only at readings of {threshold} or higher? There are {len(subset)} actual recorded entries. The observed win rate is {pct(rate)}. This is a filtered history, not a new strategy test.")
    if weekly:
        rate=100*wins(weekly)/len(weekly)
        add("Seven-day context",[f"{len(weekly)} closed",f"{rate:.1f}% wins"],f"Over seven days, {len(weekly)} trades closed. The win rate was {pct(rate)}. A higher win rate does not automatically imply a better strategy.")
    rate=100*wins(sample)/len(sample)
    add("The longer record",[f"{len(sample)} closed",f"{rate:.1f}% wins"],f"Since October twenty-eighth, {len(sample)} qualifying trades have closed. The observed win rate is {pct(rate)}. Earlier strategies are excluded from this comparison.")
    add("What did we learn?",["Signal is not certainty"],"The indicator is a signal, not a promise. Separate the entry rule from what actually follows. Use recorded outcomes to test the idea. Never assume the next move will repeat the last one.")
    add("Next episode",["Will the next signal reverse?"],"Next time, we will follow another recorded market event. Will the next signal reverse, or will the move continue? This is a trading journal, not financial advice.")
    return title,thumb,beats


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
    daily_wins = sum(x[1]>0 for x in daily)
    daily_losses = sum(x[1]<0 for x in daily)
    prior = [x for x in sample if (now-timedelta(hours=48)).timestamp()*1000 <= x[0] < (now-timedelta(hours=24)).timestamp()*1000]
    previous_pnl = sum((x[1] for x in prior), Decimal(0))
    weekly = [x for x in sample if x[0] >= (now-timedelta(days=7)).timestamp()*1000]
    weekly_pnl = sum((x[1] for x in weekly), Decimal(0))
    def trade_symbol(row):
        return str(row.get("inst") or row.get("instId") or row.get("symbol") or "Unspecified instrument")
    headline,thumbnail_text,sections=story_sections(daily,sample,opened,prior,weekly,date)
    make_thumbnail(thumbnail_text,date)
    script_words=sum(len(narration.split()) for _,_,narration,_ in sections)
    print("Story beats:",len(sections),"Narration words:",script_words)
    kokoro=Kokoro("kokoro-v1.0.onnx","voices-v1.0.bin")
    segments = []
    voice_path = OUT/"narration.wav"
    sample_rate = 24000
    frames_per_second = 24
    samples_per_frame = sample_rate // frames_per_second
    with soundfile.SoundFile(voice_path, mode="w", samplerate=sample_rate, channels=1, subtype="FLOAT") as track:
        for i, (title, body, narration, chart) in enumerate(sections):
            start = track.tell()
            for sentence in re.split(r"(?<=[.!?])\s+", narration):
                samples, rate = kokoro.create(sentence, voice="bm_george", speed=1.0, lang="en-gb", trim=False)
                if rate != sample_rate or not len(samples) or not np.isfinite(samples).all():
                    raise ValueError("Invalid synthesized sentence audio")
                track.write(samples)
            frame_count = math.ceil((track.tell() - start) / samples_per_frame)
            track.write(np.zeros(frame_count * samples_per_frame - (track.tell() - start), dtype=np.float32))
            stages=max(1,math.ceil(frame_count/(6*frames_per_second)))
            for j in range(stages):
                frames=frame_count*(j+1)//stages-frame_count*j//stages
                stage_chart=chart
                if isinstance(chart,dict) and chart.get("kind")=="replay":
                    end=chart.get("fraction",1)
                    beginning=max(0,end-.25) if title.startswith("Replay ") else end
                    stage_chart=dict(chart,fraction=beginning+(end-beginning)*(j+1)/stages)
                # Hide outcome until the last replay stage; use identical scaling.
                stage_body=body
                image=slide(title,stage_body,f"{i:02}-{j}",stage_chart)
                # Progress indicator changes with each visual beat; no fabricated market movement.
                im=Image.open(image).convert("RGB")
                draw=ImageDraw.Draw(im)
                progress=(i+(j+1)/stages)/len(sections)
                draw.rectangle((52,651,1228,655),fill="#405368")
                draw.rectangle((52,651,52+int(1176*progress),655),fill="#37D4B0")
                im.save(image)
                segment=OUT/f"seg-{i}-{j}.mp4"
                subprocess.run([FF,"-y","-v","error","-loop","1","-framerate",str(frames_per_second),
                                "-i",str(image),"-frames:v",str(frames),"-c:v","libx264",
                                "-preset","veryfast","-tune","stillimage","-pix_fmt","yuv420p",str(segment)],check=True)
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
    print("Actual narration WPM:",round(script_words/(total_samples/sample_rate/60),1))
    print("Episode duration seconds:",round(total_samples/sample_rate,1))
    if os.environ.get("REPORT_EDITION"):
        date += " | " + os.environ["REPORT_EDITION"]
    (OUT/"metadata.json").write_text(json.dumps({"title":(headline+" | "+date)[:100],"description":"BloFin Bot Replay | Episode "+now.astimezone(UK).strftime("%Y-%m-%d")+"\nHistorical results for RSI89_SHORT since 28 October 2026 only; excludes earlier strategies. Results are classified after recorded fees. RSI subsets are not backtests. Not financial or any other advice. Capital is at risk.\nSource: https://github.com/Photofiver/Stock-finder","tags":["BloFin","RSI","trading bot","crypto trading","trading journal"]},indent=2))
if __name__=="__main__":
    asyncio.run(render())
