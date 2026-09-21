#!/usr/bin/env python3
import datetime as dt
import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

UA = "Mozilla/5.0 (compatible; StrategyWatch/1.0; +https://github.com/Photofiver/Stock-finder)"
OUT = Path("strategy_watch_candidates.json")
MAX_RESULTS_PER_QUERY = 8
MAX_CANDIDATES = 200

QUERIES = [
    '"5% daily" crypto trading strategy backtest "no leverage"',
    '"5% per day" bitcoin strategy backtest spot',
    '"5 percent per day" crypto trading bot backtest spot',
    '"5% daily return" BTC backtest fees',
    'site:tradingview.com/script BTC "5% daily" backtest',
    'site:github.com bitcoin strategy "5% daily" backtest',
    'site:github.com crypto strategy "5% per day" spot backtest',
]

PATTERNS = {
    "five_percent_daily": [
        r"(?:\+?\s*5(?:\.0+)?\s*%|5\s*percent)\s*(?:a|per|each|every)?\s*(?:day|daily|24\s*(?:h|hours?))",
        r"(?:daily|per\s*day|24\s*(?:h|hours?)).{0,60}(?:\+?\s*5(?:\.0+)?\s*%|5\s*percent)",
    ],
    "backtest": [
        r"\bbacktest(?:ed|ing|s)?\b", r"walk[- ]forward", r"out[- ]of[- ]sample",
        r"historical\s+(?:test|simulation|results?)", r"strategy\s+tester"
    ],
    "no_leverage": [
        r"\bno\s+leverage\b", r"\bwithout\s+leverage\b", r"\b1x\s+leverage\b",
        r"\bleverage\s*[:=]?\s*1x\b", r"\bspot\s+(?:trading|market|strategy|btc|bitcoin|crypto)\b"
    ],
    "costs": [
        r"\bfee(?:s)?\b", r"\bcommission(?:s)?\b", r"transaction\s+cost", r"\bslippage\b"
    ],
    "rules": [
        r"\bentry\b", r"\bexit\b"
    ],
    "hourly_cap": [
        r"(?:one|1)\s+trade\s+per\s+hour", r"(?:max(?:imum)?|at\s+most)\s+(?:one|1)\s+trade\s+per\s+hour",
        r"(?:one|1)\s+position\s+per\s+hour", r"\b1h\s+timeframe\b",
        r"\b60[- ]minute\s+timeframe\b", r"\bhourly\s+timeframe\b"
    ],
    "metrics": [
        r"\bwin\s*rate\b", r"\bprofit\s*factor\b", r"\bmax(?:imum)?\s+drawdown\b",
        r"\btotal\s+trades\b", r"\bnet\s+(?:profit|return)\b"
    ],
}

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

def get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(1_500_000)
        ctype = r.headers.get("content-type", "")
        return data, ctype

def normalize_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def bing_rss(query):
    url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    try:
        data, _ = get(url)
        root = ET.fromstring(data)
    except Exception:
        return []
    out = []
    for item in root.findall(".//item")[:MAX_RESULTS_PER_QUERY]:
        title = normalize_text(item.findtext("title"))
        link = normalize_text(item.findtext("link"))
        desc = normalize_text(item.findtext("description"))
        if link.startswith("http"):
            out.append({"title": title, "url": link, "snippet": desc, "query": query})
    return out

def page_text(url):
    try:
        data, ctype = get(url)
        raw = data.decode("utf-8", errors="ignore")
        if "html" in ctype.lower() or "<html" in raw[:1000].lower():
            p = TextExtractor()
            p.feed(raw)
            return normalize_text(" ".join(p.parts))
        return normalize_text(raw)
    except Exception:
        return ""

def any_match(text, patterns):
    return any(re.search(p, text, re.I | re.S) for p in patterns)

def has_all_rules(text):
    return all(re.search(p, text, re.I | re.S) for p in PATTERNS["rules"])

def evaluate(text):
    checks = {
        "five_percent_daily": any_match(text, PATTERNS["five_percent_daily"]),
        "backtest": any_match(text, PATTERNS["backtest"]),
        "no_leverage": any_match(text, PATTERNS["no_leverage"]),
        "costs": any_match(text, PATTERNS["costs"]),
        "rules": has_all_rules(text),
        "hourly_cap": any_match(text, PATTERNS["hourly_cap"]),
        "metrics": any_match(text, PATTERNS["metrics"]),
        "dated_results": bool(re.search(r"\b20(?:1[5-9]|2[0-9])\b", text)),
    }
    return checks

def excerpt(text, patterns, radius=180):
    for p in patterns:
        m = re.search(p, text, re.I | re.S)
        if m:
            a = max(0, m.start() - radius)
            b = min(len(text), m.end() + radius)
            return text[a:b]
    return ""

def load_existing():
    if not OUT.exists():
        return {"updated_at": None, "candidates": []}
    try:
        obj = json.loads(OUT.read_text(encoding="utf-8"))
        if not isinstance(obj, dict) or not isinstance(obj.get("candidates"), list):
            raise ValueError
        return obj
    except Exception:
        return {"updated_at": None, "candidates": []}

def main():
    existing = load_existing()
    seen = {c.get("url") for c in existing["candidates"] if isinstance(c, dict)}
    search_seen = set()
    added = []

    for q in QUERIES:
        for r in bing_rss(q):
            url = r["url"]
            if url in search_seen or url in seen:
                continue
            search_seen.add(url)

            snippet = r["snippet"]
            # Only fetch pages whose search result already mentions the target return and testing.
            pre = evaluate(snippet)
            if not (pre["five_percent_daily"] and pre["backtest"]):
                continue

            text = page_text(url)
            if not text:
                continue
            checks = evaluate(text)

            # Strict gate: all requested constraints must be visible on the source page.
            if not all(checks.values()):
                continue

            key = hashlib.sha256(url.encode()).hexdigest()[:12]
            added.append({
                "id": key,
                "found_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "title": r["title"],
                "url": url,
                "query": q,
                "checks": checks,
                "evidence": {
                    "5pct_daily": excerpt(text, PATTERNS["five_percent_daily"]),
                    "backtest": excerpt(text, PATTERNS["backtest"]),
                    "no_leverage": excerpt(text, PATTERNS["no_leverage"]),
                    "hourly_cap": excerpt(text, PATTERNS["hourly_cap"]),
                    "costs": excerpt(text, PATTERNS["costs"]),
                },
                "status": "candidate_needs_independent_reproduction"
            })

    if added:
        existing["candidates"].extend(added)
        existing["candidates"] = existing["candidates"][-MAX_CANDIDATES:]
        existing["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"ADDED={len(added)}")
        for c in added:
            print(c["url"])
    else:
        print("ADDED=0")

if __name__ == "__main__":
    main()
