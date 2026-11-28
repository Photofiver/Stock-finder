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
  # Exact BloFin contract identities checked 28 Nov 2026 against CoinGecko.
  "ACE": ["Fusionist", "endurance", "Fusionist develops a blockchain-based video game and its Endurance network.", "16.73", "0.05705"],
  "SKY": ["Sky", "sky", "Sky is a decentralised finance protocol. SKY holders vote on its governance.", "0.1009", "0.03583"],
  "STABLE": ["Stable", "stable-2", "Stable is a blockchain designed for stablecoin payments and settlement.", "0.04324", "0.009221"],
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
    source = "CoinGecko | checked " + ("28 Nov 2026" if symbol in {"ACE","SKY","STABLE"} else "26 Nov 2026")
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
    """Daily market journal: actual events first, every traded project, no cash P&L."""
    def symbol(row):
        return str(row.get("inst") or row.get("instId") or row.get("symbol") or "Market").split("-")[0]
    def wins(items):
        return sum(x[1]>0 for x in items)
    def losses(items):
        return sum(x[1]<0 for x in items)
    def time_label(stamp):
        return datetime.fromtimestamp(stamp/1000,UK).strftime("%H:%M")
    def direction(row):
        entry=finite_number(row.get("open_price"))
        exit_price=finite_number(row.get("close_price"))
        if entry and exit_price:
            return (exit_price-entry)/entry*100
        return None
    def move_words(change):
        return ("rose" if change>0 else "fell" if change<0 else "was unchanged") + (f" {abs(change):.2f} percent" if change else "")
    def duration_words(minutes):
        if minutes<1:
            return f"{round(minutes*60)} seconds"
        if minutes>=120:
            return f"{minutes/60:.1f} hours"
        return f"{minutes:.1f} minutes"
    beats=[]
    def add(title,body,speech,chart=None):
        beats.append((title,body,speech,chart))
    groups={}
    for item in daily:
        groups.setdefault(symbol(item[2]),[]).append(item)
    # A real daily contrast is more useful than a recurring lesson about RSI.
    def news_rank(pair):
        sym,items=pair
        mixed=bool(wins(items) and losses(items))
        elapsed=(items[-1][0]-items[0][0])/60000
        return (mixed,len(items),-elapsed,max(abs(direction(x[2]) or 0) for x in items))
    order=sorted(groups.items(),key=news_rank,reverse=True)
    if order:
        lead,events=order[0]
        mixed=bool(wins(events) and losses(events))
        if mixed:
            title=f"{lead}: {len(events)} Trades, Two Different Outcomes"
            thumb="ONE COIN. TWO OUTCOMES"
            hook=f"{lead}: the same bot, two very different outcomes. What happened between the wins and the losses?"
        else:
            largest=max(events,key=lambda x:abs(direction(x[2]) or 0))
            change=direction(largest[2])
            title=f"{lead} Leads Today's Crypto Trading Journal"
            thumb="TODAY'S MARKET MOVES"
            hook=f"What happened to {lead} today? Its recorded trades stand out in the latest market journal."
            if change is not None:
                hook=f"Watch {lead}. Its price {move_words(change)} during one trade. But that move is only part of the picture."
        add("The last 24 hours",[f"{len(daily)} closed trades",f"{len(groups)} different markets"],hook+f" Across {len(groups)} markets, {len(daily)} trades closed. Let’s follow the price, then compare the day with yesterday.")
        # Fetch project identity and market history for every traded instrument.
        profiles={}
        for label,body,speech,chart in project_sections([str(x[2].get("inst") or x[2].get("symbol") or "") for x in daily]):
            profiles[label.split(" | ")[0]]=(body,speech,chart)
        # Lead with the distinctive market; continue across all other traded projects.
        for index,(sym,items) in enumerate(order):
            profile=profiles.get(sym)
            body,_,market_chart=profile if profile else ([],None,None)
            summary=next((b for b in body if not b.startswith(("All-time","Source:","CoinGecko","First /","5-minute","Project identity"))),"")
            market_prices=market_chart.get("prices") if market_chart else None
            chart=market_chart if market_prices else None
            intro=([f"Now switch markets to {sym}.", f"Over on {sym}, the picture changes.", f"Look at {sym} next.", f"That brings us to {sym}."][index%4] if len(items)==1 else f"{sym} deserves a closer look: {len(items)} trades closed.")
            if index==0:
                intro=f"Start with {sym}. Look at the chart."
                if len(items)>1:
                    span=(items[-1][0]-items[0][0])/60000
                    intro+=f" Its {len(items)} trades closed within {duration_words(span)}."
            if summary:
                # Use verified source wording, broken into short clauses when practical.
                spoken=" ".join(v for v in re.split(r"(?<=[.!?])\s+",summary) if len(v.split())<=20)
                intro+=" "+(spoken or f"The verified project description is shown here.")
            else:
                intro+=" The project identity could not be confirmed."
            add(f"{sym} | Project",([summary] if summary else ["Project identity unverified"])+[f"{len(items)} closed trades"],intro,chart)
            # Separate actual trade outcome from the market-wide move.
            for k,(closed,p,row) in enumerate(items,1):
                change=direction(row)
                hold=finite_number(row.get("hold_minutes"))
                rsi=finite_number((row.get("signal_snapshot") or {}).get("rsi14"))
                entry=finite_number(row.get("open_price"))
                exit_price=finite_number(row.get("close_price"))
                result="Win" if p>0 else "Loss" if p<0 else "Break-even"
                clock=time_label(closed)
                leadline=f"By {clock}, the short was over."
                if k>1: leadline=f"Then came the next close, at {clock}."
                details=[]
                spoken=leadline
                if hold is not None and hold>=0:
                    spoken+=f" The position had been open for {duration_words(hold)}."
                    details.append(duration_words(hold))
                if rsi is not None:
                    details.append(f"Entry RSI {rsi:.1f}")
                if entry and exit_price:
                    details.append(f"{entry:.7g} to {exit_price:.7g}")
                bars=trade_candles(row)
                replay=None
                if bars and entry and exit_price and row.get("opened_ms") and row.get("closed_ms"):
                    replay={"kind":"replay","row":row,"candles":bars,"fraction":1,"reveal":False}
                # No replay of a partial five-minute candle is fabricated.
                add(f"{sym} | Trade {k}",[clock+" UK"]+details[:2],spoken,replay or chart)
                spoken=(f"The price {move_words(change)}. " if change is not None else "The exact price move is unavailable. ")
                alternatives = (["A win for the short.", "That trade finished in profit after fees.", "The short closed with a win."] if p>0 else ["But the short lost.", "That one went against the bot.", "The recorded result: a loss."] if p<0 else ["The result was break-even."])
                spoken+=alternatives[(index+k)%len(alternatives)]
                if k<len(items):
                    next_p=items[k][1]
                    if (p>0 and next_p<0) or (p<0 and next_p>0):
                        spoken+=" Now watch what happened on the next trade." 
                if (row.get("post_trade_review") or {}).get("had_gross_profit_before_net_loss") and p<0:
                    spoken+=" An earlier favourable move did not last."
                if replay:
                    replay=dict(replay,reveal=True)
                add(f"{sym} | {result}",[result+" after fees"]+([f"Coin move {change:+.2f}%"] if change is not None else []),spoken,replay or chart)
            records=[b for b in body if b.startswith(("All-time high","All-time low"))]
            if records:
                source=next((b for b in body if b.startswith(("CoinGecko","Source:"))),"Source: CoinGecko")
                add(f"{sym} | Price history",records+[source],[f"Zoom out. Here are {sym}'s historical extremes.", "For perspective, check the recorded high and low.", "The price history gives us a wider frame.", "Here is the longer-term price range."][index%4],chart)
            if market_prices:
                market_change=(market_prices[-1][1]/market_prices[0][1]-1)*100
                moves=[direction(x[2]) for x in items]
                contrast=any(m is not None and m*market_change<0 for m in moves)
                market_speech=(f"Here is the contrast: the wider chart {move_words(market_change)}, in the opposite direction to at least one trade." if contrast else [f"Across the wider chart, {sym} {move_words(market_change)}.",f"Now zoom out to the available daily candles: the price {move_words(market_change)}.",f"The broader market move? It {move_words(market_change)}."][index%3])
                add(f"{sym} | Market today",[f"{market_change:+.2f}%","Available 5-minute closes (market, not trade return)"],market_speech,chart)
            else:
                add(f"{sym} | Market today",["Daily chart unavailable"],"The current market chart could not be retrieved. The trade records remain separate from this missing data.")
    else:
        title="Crypto Trading Journal: No New Closes"
        thumb="NO NEW CLOSES"
        add("The last 24 hours",["No qualifying closed trades"],"No qualifying trade closed in the latest twenty-four hours. There is no completed trade to replay today.")
    w,l=wins(daily),losses(daily)
    rate=100*w/len(daily) if daily else 0
    add("Today's results",[f"{w} wins / {l} losses",f"{rate:.1f}% wins" if daily else "No win rate"],f"So where did the day finish? {w} wins, {l} losses."+ (f" The win rate was {rate:.1f} percent." if daily else " No daily win rate can be calculated.")+f" There are {len(opened)} positions still open.")
    if prior:
        prev=100*wins(prior)/len(prior)
        add("Yesterday compared",[f"Yesterday {len(prior)} trades",f"Today {len(daily)} trades"],f"Yesterday, {len(prior)} trades closed with a {prev:.1f} percent win rate. Today's activity {'increased' if len(daily)>len(prior) else 'decreased' if len(daily)<len(prior) else 'was unchanged'}.")
    if weekly:
        weekrate=100*wins(weekly)/len(weekly)
        add("Seven-day record",[f"{len(weekly)} closed",f"{weekrate:.1f}% wins"],f"Over the past seven days, {len(weekly)} trades closed. The observed win rate was {weekrate:.1f} percent.")
    rate_all=100*wins(sample)/len(sample)
    add("Overall record",[f"{len(sample)} closed",f"{rate_all:.1f}% wins"],f"The current strategy has {len(sample)} recorded closed trades since October twenty-eighth. Its observed win rate is {rate_all:.1f} percent.")
    add("Tomorrow",["New events. New markets."],"Tomorrow, we will check whether the pattern holds or breaks. These are recorded results, not a forecast.")
    print("Daily markets covered:",",".join(sym for sym,_ in order),"Closed trades covered:",len(daily))
    for label,_,speech,_ in beats:
        print("SCRIPT:",label,"|",speech)
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
    sentence_lengths=[len(v.split()) for _,_,narration,_ in sections for v in re.split(r"(?<=[.!?])\s+",narration) if v.strip()]
    print("Story beats:",len(sections),"Narration words:",script_words,"Longest sentence:",max(sentence_lengths))
    kokoro=Kokoro("kokoro-v1.0.onnx","voices-v1.0.bin")
    segments = []
    voice_path = OUT/"narration.wav"
    sample_rate = 24000
    frames_per_second = 24
    samples_per_frame = sample_rate // frames_per_second
    with soundfile.SoundFile(voice_path, mode="w", samplerate=sample_rate, channels=1, subtype="FLOAT") as track:
        for i, (title, body, narration, chart) in enumerate(sections):
            start = track.tell()
            sentences = [v for v in re.split(r"(?<=[.!?])\s+", narration) if v.strip()]
            chunks=[]
            for sentence in sentences:
                samples, rate = kokoro.create(sentence, voice="bm_george", speed=(1.04 if sentence.endswith("?") else 1.13 if len(sentence.split())>9 else 1.08), lang="en-gb", trim=True)
                if rate != sample_rate or not len(samples) or not np.isfinite(samples).all():
                    raise ValueError("Invalid synthesized sentence audio")
                chunks.append(samples.astype(np.float32))
                if sentence != sentences[-1]:
                    chunks.append(np.zeros(int(sample_rate*(.13 if sentence.endswith("?") else .07)),dtype=np.float32))
            spoken=np.concatenate(chunks)
            word_count=len(narration.split())
            wpm=word_count*60*sample_rate/len(spoken)
            if wpm<155 or wpm>170:
                factor=max(.72,min(1.75,162/wpm))
                paced=subprocess.run([FF,"-v","error","-f","f32le","-ar",str(sample_rate),"-ac","1",
                                      "-i","pipe:0","-af",f"atempo={factor:.5f}",
                                      "-f","f32le","pipe:1"],input=spoken.astype("<f4").tobytes(),
                                      capture_output=True,check=True)
                adjusted=np.frombuffer(paced.stdout,dtype="<f4")
                if len(adjusted) and np.isfinite(adjusted).all(): spoken=adjusted
            if title.endswith((" | Win"," | Loss"," | Break-even")):
                track.write(np.zeros(sample_rate//8,dtype=np.float32))
            track.write(spoken)
            frame_count = math.ceil((track.tell() - start) / samples_per_frame)
            track.write(np.zeros(frame_count * samples_per_frame - (track.tell() - start), dtype=np.float32))
            stages=max(1,math.ceil(frame_count/(3*frames_per_second)))
            for j in range(stages):
                frames=frame_count*(j+1)//stages-frame_count*j//stages
                stage_chart=chart
                if isinstance(chart,dict) and chart.get("kind")=="replay":
                    end=chart.get("fraction",1)
                    stage_chart=dict(chart,fraction=end if chart.get("reveal") else end*(j+1)/stages)
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
    actual_wpm=script_words/(total_samples/sample_rate/60)
    print("Actual narration WPM:",round(actual_wpm,1))
    if not 145 <= actual_wpm <= 175:
        raise RuntimeError("Episode narration pacing outside acceptable range")
    print("Episode duration seconds:",round(total_samples/sample_rate,1))
    if os.environ.get("REPORT_EDITION"):
        date += " | " + os.environ["REPORT_EDITION"]
    (OUT/"metadata.json").write_text(json.dumps({"title":(headline+" | "+date)[:100],"description":"BloFin Bot Replay | Episode "+now.astimezone(UK).strftime("%Y-%m-%d")+"\nHistorical results for RSI89_SHORT since 28 October 2026 only; excludes earlier strategies. Daily market events and verified project context. Results are classified after recorded fees. Not financial or any other advice. Capital is at risk.\nSource: https://github.com/Photofiver/Stock-finder","tags":["BloFin","RSI","trading bot","crypto trading","trading journal"]},indent=2))
if __name__=="__main__":
    asyncio.run(render())
