#!/usr/bin/env python3
import datetime as dt
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

UA = "Mozilla/5.0 (compatible; StrategyWatch/1.1; +https://github.com/Photofiver/Stock-finder)"
CANDIDATES = Path("strategy_watch_candidates.json")
HISTORY = Path("strategy_watch_history.jsonl")
STATE = Path("strategy_watch_state.json")
MAX_CANDIDATES = 200
RESULTS_PER_RUN = 8

QUERIES = [
    '"5% daily" crypto trading strategy backtest "no leverage"',
    '"5% per day" bitcoin strategy backtest spot',
    '"5 percent per day" crypto trading bot backtest spot',
    '"5% daily return" BTC backtest fees',
    'site:tradingview.com/script BTC "5% daily" backtest',
    'site:github.com bitcoin strategy "5% daily" backtest',
    'site:github.com crypto strategy "5% per day" spot backtest',
    '"bitcoin" "backtest" "1x" "entry" "exit" "fees"',
    '"BTC" "one trade per hour" backtest strategy',
    '"crypto" "hourly timeframe" "5% daily" strategy backtest',
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
    "costs": [r"\bfee(?:s)?\b", r"\bcommission(?:s)?\b", r"transaction\s+cost", r"\bslippage\b"],
    "hourly_cap": [
        r"(?:one|1)\s+trade\s+per\s+hour",
        r"(?:max(?:imum)?|at\s+most)\s+(?:one|1)\s+trade\s+per\s+hour",
        r"(?:one|1)\s+position\s+per\s+hour",
        r"\b1h\s+timeframe\b", r"\b60[- ]minute\s+timeframe\b", r"\bhourly\s+timeframe\b"
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

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(1_500_000), r.headers.get("content-type", "")

def norm(s):
    s = html.unescape(s or "")
    return re.sub(r"\s+", " ", s).strip()

def bing_rss(query, first=1):
    url = "https://www.bing.com/search?format=rss&count=8&first=" + str(first) + "&q=" + urllib.parse.quote(query)
    try:
        data, _ = get(url)
        root = ET.fromstring(data)
    except Exception as e:
        return [], str(e)
    out = []
    for item in root.findall(".//item")[:RESULTS_PER_RUN]:
        link = norm(item.findtext("link"))
        if link.startswith("http"):
            out.append({
                "title": norm(item.findtext("title")),
                "url": link,
                "snippet": norm(item.findtext("description")),
            })
    return out, None

def page_text(url):
    try:
        data, ctype = get(url)
        raw = data.decode("utf-8", errors="ignore")
        if "html" in ctype.lower() or "<html" in raw[:1000].lower():
            p = TextExtractor()
            p.feed(raw)
            return norm(" ".join(p.parts)), None
        return norm(raw), None
    except Exception as e:
        return "", str(e)

def any_match(text, pats):
    return any(re.search(p, text, re.I | re.S) for p in pats)

def evaluate(text):
    return {
        "five_percent_daily": any_match(text, PATTERNS["five_percent_daily"]),
        "backtest": any_match(text, PATTERNS["backtest"]),
        "no_leverage": any_match(text, PATTERNS["no_leverage"]),
        "costs": any_match(text, PATTERNS["costs"]),
        "rules": bool(re.search(r"\bentry\b", text, re.I) and re.search(r"\bexit\b", text, re.I)),
        "hourly_cap": any_match(text, PATTERNS["hourly_cap"]),
        "metrics": any_match(text, PATTERNS["metrics"]),
        "dated_results": bool(re.search(r"\b20(?:1[5-9]|2[0-9])\b", text)),
    }

def excerpt(text, pats, radius=160):
    for p in pats:
        m = re.search(p, text, re.I | re.S)
        if m:
            return text[max(0,m.start()-radius):min(len(text),m.end()+radius)]
    return ""

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def append_history(obj):
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def main():
    state = load_json(STATE, {"run_count": 0, "seen_urls": [], "evaluated_count": 0})
    candidates = load_json(CANDIDATES, {"updated_at": None, "candidates": []})
    seen = set(state.get("seen_urls", []))

    run_count = int(state.get("run_count", 0))
    q_index = run_count % len(QUERIES)
    page_cycle = run_count // len(QUERIES)
    first = 1 + (page_cycle % 20) * RESULTS_PER_RUN
    query = QUERIES[q_index]

    results, search_error = bing_rss(query, first)
    chosen = None
    for r in results:
        if r["url"] not in seen:
            chosen = r
            break

    if chosen is None:
        append_history({
            "run_at_utc": now(),
            "query": query,
            "first": first,
            "status": "no_new_result",
            "search_error": search_error,
        })
    else:
        url = chosen["url"]
        seen.add(url)
        text, fetch_error = page_text(url)
        checks = evaluate(text if text else (chosen["title"] + " " + chosen["snippet"]))
        passed = bool(text) and all(checks.values())
        missing = [k for k,v in checks.items() if not v]
        record = {
            "run_at_utc": now(),
            "query": query,
            "first": first,
            "title": chosen["title"],
            "url": url,
            "checks": checks,
            "passed_strict_filter": passed,
            "missing": missing,
            "fetch_error": fetch_error,
        }
        append_history(record)
        state["evaluated_count"] = int(state.get("evaluated_count", 0)) + 1

        if passed:
            candidate = {
                "id": hashlib.sha256(url.encode()).hexdigest()[:12],
                "found_at_utc": now(),
                "title": chosen["title"],
                "url": url,
                "query": query,
                "checks": checks,
                "evidence": {
                    "5pct_daily": excerpt(text, PATTERNS["five_percent_daily"]),
                    "backtest": excerpt(text, PATTERNS["backtest"]),
                    "no_leverage": excerpt(text, PATTERNS["no_leverage"]),
                    "hourly_cap": excerpt(text, PATTERNS["hourly_cap"]),
                    "costs": excerpt(text, PATTERNS["costs"]),
                },
                "status": "candidate_needs_independent_reproduction"
            }
            existing_urls = {c.get("url") for c in candidates.get("candidates", []) if isinstance(c, dict)}
            if url not in existing_urls:
                candidates.setdefault("candidates", []).append(candidate)
                candidates["candidates"] = candidates["candidates"][-MAX_CANDIDATES:]
                candidates["updated_at"] = now()
                save_json(CANDIDATES, candidates)

    state["run_count"] = run_count + 1
    state["seen_urls"] = list(seen)[-5000:]
    state["last_run_at_utc"] = now()
    state["last_query"] = query
    state["last_first"] = first
    save_json(STATE, state)
    print(json.dumps({
        "run_count": state["run_count"],
        "evaluated_count": state.get("evaluated_count", 0),
        "candidate_count": len(candidates.get("candidates", [])),
        "query": query,
        "first": first
    }))

if __name__ == "__main__":
    main()
