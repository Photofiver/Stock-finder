import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

BLOFIN_BASE = "https://openapi.blofin.com"
COINPAPRIKA_URL = "https://api.coinpaprika.com/v1/tickers"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "blofin-nhd0jt7wspfnhtitdlaowk1n").strip()

TIMEFRAME = "1H"
TOP_N = 10
REPORT_N = 10
WORKERS = 4
REQUEST_INTERVAL = 0.15
HTTP_RETRIES = 4
STATE_FILE = "scanner_state.json"

NON_CRYPTO_SYMBOLS = {
    "AAPL", "AMZN", "AVGO", "CL", "COIN", "GOOGL", "META", "MSFT", "MSTR",
    "NATGAS", "NG", "NVDA", "OIL", "TSLA", "WTIOIL", "XLE", "XOM",
}

_request_lock = threading.Lock()
_last_request_started = 0.0


def wait_for_blofin_slot():
    global _last_request_started
    with _request_lock:
        now = time.monotonic()
        wait = REQUEST_INTERVAL - (now - _last_request_started)
        if wait > 0:
            time.sleep(wait)
        _last_request_started = time.monotonic()


def blofin_get(path, params=None):
    last_error = None
    for attempt in range(HTTP_RETRIES):
        try:
            wait_for_blofin_slot()
            response = requests.get(BLOFIN_BASE + path, params=params, timeout=20)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                pause = 1.0 + attempt
                if retry_after:
                    try:
                        pause = max(float(retry_after), pause)
                    except ValueError:
                        pass
                time.sleep(pause)
                raise requests.HTTPError("HTTP 429", response=response)
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("code")) != "0":
                raise RuntimeError(f"BloFin API error: {payload}")
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise RuntimeError("BloFin returned non-list data")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"BloFin request failed after {HTTP_RETRIES} attempts: {last_error}")


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if not any(v is None for v in window):
            out[i] = sum(window) / period
    return out


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2 / (period + 1)
    previous = sum(values[:period]) / period
    out[period - 1] = previous
    for i in range(period, len(values)):
        previous = alpha * values[i] + (1 - alpha) * previous
        out[i] = previous
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) < period + 1:
        return out

    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        out[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    return out


def stochastic(highs, lows, closes, period=14, smooth_k=3, smooth_d=3):
    raw_k = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        high = max(highs[i - period + 1:i + 1])
        low = min(lows[i - period + 1:i + 1])
        raw_k[i] = 50.0 if high == low else 100 * (closes[i] - low) / (high - low)
    k = sma(raw_k, smooth_k)
    d = sma(k, smooth_d)
    return k, d


def macd_histogram(values, fast=12, slow=26, signal=9):
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)

    macd_line = [None] * len(values)
    compact = []
    positions = []
    for i, (fast_value, slow_value) in enumerate(zip(fast_ema, slow_ema)):
        if fast_value is not None and slow_value is not None:
            value = fast_value - slow_value
            macd_line[i] = value
            compact.append(value)
            positions.append(i)

    compact_signal = ema(compact, signal)
    histogram = [None] * len(values)
    for j, i in enumerate(positions):
        if compact_signal[j] is not None:
            histogram[i] = macd_line[i] - compact_signal[j]

    return histogram


def parse_candles(raw):
    candles = []
    for row in raw:
        try:
            confirm = str(row[8]) if len(row) > 8 else "0"
            candles.append(
                (
                    int(row[0]),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                    confirm,
                )
            )
        except (TypeError, ValueError, IndexError):
            continue
    candles.sort(key=lambda row: row[0])
    return candles


def get_live_instruments():
    live = []
    for row in blofin_get("/api/v1/market/instruments"):
        if (
            row.get("state") == "live"
            and row.get("instType") == "SWAP"
            and row.get("contractType") == "linear"
            and row.get("settleCurrency") == "USDT"
        ):
            inst = str(row.get("instId") or "")
            if inst:
                live.append(inst)
    return sorted(set(live))


def get_universe():
    live = get_live_instruments()
    live_set = set(live)
    ranked = []
    ranking_errors = []

    for ticker in blofin_get("/api/v1/market/tickers"):
        inst = str(ticker.get("instId") or "")
        if inst not in live_set:
            continue
        try:
            last = float(ticker.get("last") or 0)
            open_24h = float(ticker.get("open24h") or 0)
        except (TypeError, ValueError):
            ranking_errors.append(f"{inst}: nieprawidłowe dane 24h")
            continue
        if last <= 0 or open_24h <= 0:
            ranking_errors.append(f"{inst}: brak poprawnej ceny 24h")
            continue
        change_24h = (last / open_24h - 1) * 100
        ranked.append((change_24h, inst))

    ranked.sort(key=lambda item: item[0], reverse=True)
    ranked = ranked[:TOP_N]

    coins = [
        {"inst": inst, "change": change, "blofin_rank": rank}
        for rank, (change, inst) in enumerate(ranked, start=1)
    ]
    return coins, live, ranking_errors


def get_closed_candles(inst):
    raw = blofin_get(
        "/api/v1/market/candles",
        {"instId": inst, "bar": TIMEFRAME, "limit": "120"},
    )
    return [row for row in parse_candles(raw) if row[6] == "1"]


def fetch_market_caps():
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(
                COINPAPRIKA_URL,
                params={"quotes": "USD"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError("CoinPaprika returned non-list data")

            by_symbol = {}
            for row in payload:
                try:
                    symbol = str(row.get("symbol") or "").upper().strip()
                    rank = int(row.get("rank") or 999999)
                    market_cap = float(
                        row.get("quotes", {}).get("USD", {}).get("market_cap") or 0
                    )
                except (TypeError, ValueError, AttributeError):
                    continue

                if not symbol or market_cap <= 0:
                    continue

                candidate = (rank, -market_cap, market_cap)
                current = by_symbol.get(symbol)
                if current is None or candidate[:2] < current[:2]:
                    by_symbol[symbol] = candidate

            return {symbol: values[2] for symbol, values in by_symbol.items()}
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 + attempt)

    raise RuntimeError(f"CoinPaprika request failed: {last_error}")


def market_cap_for_instrument(inst, market_caps):
    base = inst.split("-", 1)[0].upper()

    if base in NON_CRYPTO_SYMBOLS:
        return None
    if base in market_caps:
        return market_caps[base]
    if base.startswith("1000") and base[4:] in market_caps:
        return market_caps[base[4:]]

    return None


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return {"last_slot": None, "captured_at": None, "market_caps": {}}

        market_caps = {}
        for key, value in (payload.get("market_caps") or {}).items():
            try:
                number = float(value)
                if number > 0:
                    market_caps[str(key)] = number
            except (TypeError, ValueError):
                continue

        return {
            "last_slot": payload.get("last_slot"),
            "captured_at": payload.get("captured_at"),
            "market_caps": market_caps,
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"last_slot": None, "captured_at": None, "market_caps": {}}


def current_slot():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:00Z")


def save_state(slot, market_caps):
    payload = {
        "last_slot": slot,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "market_caps": market_caps,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def analyze(coin, current_mc, previous_mc):
    inst = coin["inst"]
    candles = get_closed_candles(inst)
    if len(candles) < 40:
        raise RuntimeError(f"za mało zamkniętych świec ({len(candles)})")

    opens = [row[1] for row in candles]
    highs = [row[2] for row in candles]
    lows = [row[3] for row in candles]
    closes = [row[4] for row in candles]
    volumes = [row[5] for row in candles]

    hist = macd_histogram(closes)
    rs = rsi(closes)
    k, d = stochastic(highs, lows, closes)

    i = len(candles) - 1
    p = i - 1

    for series in (hist, rs, k, d):
        if series[p] is None or series[i] is None:
            raise RuntimeError("brak danych wskaźników")

    short_flip = hist[p] > 0 and hist[i] < 0
    long_flip = hist[p] < 0 and hist[i] > 0

    mc_available = (
        current_mc is not None
        and previous_mc is not None
        and previous_mc > 0
    )
    mc_up = mc_available and current_mc > previous_mc
    mc_down = mc_available and current_mc < previous_mc

    short_rules = [
        short_flip,
        closes[i] < opens[i],
        closes[p] > opens[p],
        volumes[i] > volumes[p],
        rs[i] < rs[p],
        k[p] >= 80 and k[i] < k[p],
        k[i] < d[i],
        mc_down,
    ]

    long_rules = [
        long_flip,
        closes[i] > opens[i],
        closes[p] < opens[p],
        volumes[i] > volumes[p],
        rs[i] > rs[p],
        k[p] <= 20 and k[i] > k[p],
        k[i] > d[i],
        mc_up,
    ]

    short_score = sum(short_rules)
    long_score = sum(long_rules)

    if short_score >= long_score:
        side = "SHORT"
        score = short_score
        selected_flip = short_flip
    else:
        side = "LONG"
        score = long_score
        selected_flip = long_flip

    if short_flip:
        flip_label = "MACD ZIELONY→CZERWONY"
    elif long_flip:
        flip_label = "MACD CZERWONY→ZIELONY"
    else:
        flip_label = "MACD bez świeżej zmiany"

    mc_change = None
    if mc_available:
        mc_change = (current_mc / previous_mc - 1) * 100

    result = dict(coin)
    result.update(
        {
            "side": side,
            "score": score,
            "exact": score == 8,
            "macd_flip": selected_flip,
            "flip_label": flip_label,
            "rsi": rs[i],
            "prev_k": k[p],
            "k": k[i],
            "d": d[i],
            "market_cap": current_mc,
            "market_cap_change": mc_change,
        }
    )
    return result


def format_market_cap(value):
    if value is None:
        return "niedostępny"
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def format_market_cap_status(row):
    current = row.get("market_cap")
    change = row.get("market_cap_change")

    if current is None:
        return "MC niedostępny"
    if change is None:
        return f"MC {format_market_cap(current)} | brak historii 1H"

    arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
    return f"MC {format_market_cap(current)} {arrow} {change:+.2f}%/1H"


def send_ntfy(message):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC is empty")

    last_error = None
    for attempt in range(5):
        try:
            response = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={
                    "Title": "BloFin 1H Scanner",
                    "Priority": "4",
                },
                timeout=20,
            )
            response.raise_for_status()
            try:
                message_id = response.json().get("id")
            except (ValueError, AttributeError):
                message_id = None
            suffix = f" id={message_id}" if message_id else ""
            print(f"NTFY OK{suffix}")
            return
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"ntfy failed after 5 attempts: {last_error}")


def build_message(coins, results, errors, ranking_errors, marketcap_error):
    results.sort(
        key=lambda row: (
            row["macd_flip"],
            row["score"],
            -row["blofin_rank"],
        ),
        reverse=True,
    )

    exact = [row for row in results if row["exact"]]
    chosen = exact if exact else results[:REPORT_N]
    flips = sum(1 for row in results if row["macd_flip"])

    if exact:
        header = (
            f"TOP {len(coins)} BloFin wg 24h Change | analiza 1H | "
            f"świeże MACD: {flips} | PEŁNY SETUP 8/8: {len(exact)}"
        )
    else:
        header = (
            f"TOP {len(coins)} BloFin wg 24h Change | analiza 1H | "
            f"świeże MACD: {flips} | najlepsze {len(chosen)}"
        )

    lines = []
    for n, row in enumerate(chosen, start=1):
        lines.append(
            f"{n}. {row['inst']} {row['side']} — {row['score']}/8 | "
            f"{row['flip_label']} | BloFin 24h rank #{row['blofin_rank']} "
            f"{row['change']:+.2f}%/24h | RSI {row['rsi']:.1f} | "
            f"STOCH K {row['prev_k']:.1f}→{row['k']:.1f}, D {row['d']:.1f} | "
            f"{format_market_cap_status(row)}"
        )

    if not lines:
        lines.append("Brak instrumentów z wystarczającymi danymi.")

    if marketcap_error:
        lines.append("MC chwilowo niedostępny — pozostałe 7 warunków policzone.")
    if errors:
        lines.append(f"Pominięto {len(errors)} instrumentów z TOP 10 podczas analizy 1H.")
    if ranking_errors:
        lines.append(f"Pominięto {len(ranking_errors)} instrumentów z błędnymi danymi 24h.")

    return header + "\n" + "\n".join(lines)


def main():
    force_scan = os.getenv("FORCE_SCAN", "0") == "1"
    no_state = os.getenv("NO_STATE", "0") == "1"

    state = load_state()
    slot = current_slot()

    if state.get("last_slot") == slot and not force_scan:
        print(f"SKIP: scan for {slot} already completed")
        return 0

    coins, live_instruments, ranking_errors = get_universe()
    if not coins:
        raise RuntimeError("Nie znaleziono aktywnych kontraktów USDT-M z danymi 24h")

    previous_market_caps = state.get("market_caps") or {}

    marketcap_error = None
    try:
        by_symbol = fetch_market_caps()
    except Exception as exc:
        by_symbol = {}
        marketcap_error = f"{type(exc).__name__}: {exc}"
        print(f"MARKET CAP ERROR: {marketcap_error}", file=sys.stderr)

    current_market_caps = {
        inst: market_cap_for_instrument(inst, by_symbol)
        for inst in live_instruments
    }

    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_map = {
            executor.submit(
                analyze,
                coin,
                current_market_caps.get(coin["inst"]),
                previous_market_caps.get(coin["inst"]),
            ): coin["inst"]
            for coin in coins
        }

        for future in as_completed(future_map):
            inst = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append(f"{inst}: {type(exc).__name__}: {exc}")

    for error in ranking_errors:
        print(f"RANKING 24H: {error}", file=sys.stderr)
    for error in errors:
        print(error, file=sys.stderr)

    message = build_message(coins, results, errors, ranking_errors, marketcap_error)
    send_ntfy(message)
    print(message)

    if not no_state:
        state_values = {
            inst: value
            for inst, value in current_market_caps.items()
            if value is not None and value > 0
        }
        save_state(slot, state_values)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        error_message = f"BŁĄD SKANERA: {type(exc).__name__}: {exc}"
        print(error_message, file=sys.stderr)
        try:
            send_ntfy(error_message)
        except Exception as notify_exc:
            print(f"NTFY ERROR: {notify_exc}", file=sys.stderr)
        raise
