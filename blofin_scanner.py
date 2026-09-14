import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

BASE = 'https://openapi.blofin.com'
COINPAPRIKA_TICKERS = 'https://api.coinpaprika.com/v1/tickers'
TIMEFRAME = '1H'
TOP_N = 200
REPORT_N = 10
WORKERS = 4
RETRIES = 4
REQUEST_INTERVAL = 0.15
MARKETCAP_STATE_FILE = 'marketcap_state.json'
NTFY_TOPIC = os.getenv('NTFY_TOPIC', 'blofin-nhd0jt7wspfnhtitdlaowk1n').strip()

# BloFin also lists some tokenized stocks/commodities. Do not map those symbols
# to unrelated crypto projects that happen to use the same ticker.
NON_CRYPTO_SYMBOLS = {
    'AAPL', 'AMZN', 'AVGO', 'COIN', 'GOOGL', 'META', 'MSFT', 'MSTR',
    'NATGAS', 'NG', 'NVDA', 'OIL', 'TSLA', 'WTIOIL', 'XLE', 'XOM',
}

_request_lock = threading.Lock()
_last_request_at = 0.0


def wait_for_request_slot():
    global _last_request_at
    with _request_lock:
        now = time.monotonic()
        delay = REQUEST_INTERVAL - (now - _last_request_at)
        if delay > 0:
            time.sleep(delay)
        _last_request_at = time.monotonic()


def api_get(path, params=None):
    last_error = None
    for attempt in range(RETRIES):
        try:
            wait_for_request_slot()
            response = requests.get(BASE + path, params=params, timeout=20)
            if response.status_code == 429:
                retry_after = response.headers.get('Retry-After')
                if retry_after:
                    try:
                        time.sleep(max(float(retry_after), 1.0))
                    except ValueError:
                        pass
                raise requests.HTTPError('HTTP 429', response=response)
            if response.status_code >= 500:
                raise requests.HTTPError(f'HTTP {response.status_code}', response=response)
            response.raise_for_status()
            payload = response.json()
            if str(payload.get('code')) != '0':
                raise RuntimeError(f"BloFin API error: {payload}")
            return payload.get('data', [])
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt == RETRIES - 1:
                break
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f'BloFin request failed after {RETRIES} attempts: {last_error}')


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
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0)) / period
        out[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if not any(x is None for x in window):
            out[i] = sum(window) / period
    return out


def stochastic(highs, lows, closes, k_period=14, smooth_k=3, d_period=3):
    raw = [None] * len(closes)
    for i in range(k_period - 1, len(closes)):
        highest = max(highs[i - k_period + 1:i + 1])
        lowest = min(lows[i - k_period + 1:i + 1])
        raw[i] = 50 if highest == lowest else 100 * (closes[i] - lowest) / (highest - lowest)
    k = sma(raw, smooth_k)
    d = sma(k, d_period)
    return k, d


def macd_histogram(values, fast=12, slow=26, signal=9):
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line = [None] * len(values)
    for i in range(len(values)):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            line[i] = fast_ema[i] - slow_ema[i]
    valid_line = [x for x in line if x is not None]
    signal_line = ema(valid_line, signal)
    histogram = [None] * len(values)
    j = 0
    for i, value in enumerate(line):
        if value is not None:
            if signal_line[j] is not None:
                histogram[i] = value - signal_line[j]
            j += 1
    return histogram


def get_universe():
    live = set()
    for row in api_get('/api/v1/market/instruments'):
        if (
            row.get('state') == 'live'
            and row.get('instType') == 'SWAP'
            and row.get('contractType') == 'linear'
            and row.get('settleCurrency') == 'USDT'
        ):
            live.add(row.get('instId', ''))

    ranked = []
    for ticker in api_get('/api/v1/market/tickers'):
        inst = ticker.get('instId', '')
        if inst not in live:
            continue
        try:
            last = float(ticker.get('last') or 0)
            open_24h = float(ticker.get('open24h') or 0)
            if last > 0 and open_24h > 0:
                ranked.append(((last / open_24h - 1) * 100, inst))
        except (TypeError, ValueError):
            continue

    ranked.sort(key=lambda x: x[0], reverse=True)
    ranked = ranked[:TOP_N]
    return [
        {'inst': inst, 'change': change, 'blofin_rank': rank}
        for rank, (change, inst) in enumerate(ranked, 1)
    ]


def get_closed_candles(inst):
    rows = []
    raw = api_get('/api/v1/market/candles', {'instId': inst, 'bar': TIMEFRAME, 'limit': '120'})
    for candle in raw:
        try:
            rows.append((
                int(candle[0]),
                float(candle[1]),
                float(candle[2]),
                float(candle[3]),
                float(candle[4]),
                float(candle[5]),
                str(candle[8]) if len(candle) > 8 else '0',
            ))
        except (ValueError, TypeError, IndexError):
            continue
    rows.sort(key=lambda x: x[0])
    return [row for row in rows if row[6] == '1']


def load_marketcap_state():
    try:
        with open(MARKETCAP_STATE_FILE, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        values = payload.get('market_caps', {})
        if isinstance(values, dict):
            return {str(k): float(v) for k, v in values.items() if v is not None and float(v) > 0}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return {}


def fetch_market_caps():
    response = requests.get(COINPAPRIKA_TICKERS, params={'quotes': 'USD'}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError('Nieprawidłowa odpowiedź CoinPaprika')

    by_symbol = {}
    for row in payload:
        try:
            symbol = str(row.get('symbol') or '').upper().strip()
            rank = int(row.get('rank') or 999999)
            market_cap = float(row.get('quotes', {}).get('USD', {}).get('market_cap') or 0)
            if not symbol or market_cap <= 0:
                continue
            current = by_symbol.get(symbol)
            # With duplicate tickers choose the higher-ranked project; market cap is
            # used as a tie-breaker if ranks are equal/unknown.
            candidate = (rank, -market_cap, market_cap)
            if current is None or candidate[:2] < current[:2]:
                by_symbol[symbol] = candidate
        except (TypeError, ValueError, AttributeError):
            continue

    return {symbol: data[2] for symbol, data in by_symbol.items()}


def market_cap_for_instrument(inst, market_caps):
    base = inst.split('-', 1)[0].upper()
    if base in NON_CRYPTO_SYMBOLS:
        return None
    if base in market_caps:
        return market_caps[base]
    if base.startswith('1000') and base[4:] in market_caps:
        return market_caps[base[4:]]
    return None


def should_save_hourly_state():
    # Normal trigger runs at :01. Keep only an hourly baseline so manual tests
    # do not replace it with a snapshot taken in the middle of the hour.
    return datetime.now(timezone.utc).minute <= 5


def save_marketcap_state(current_market_caps):
    payload = {
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'market_caps': current_market_caps,
    }
    with open(MARKETCAP_STATE_FILE, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write('\n')


def analyze(inst, current_mc=None, previous_mc=None):
    candles = get_closed_candles(inst)
    if len(candles) < 40:
        raise RuntimeError(f'za mało zamkniętych świec ({len(candles)})')

    opens = [x[1] for x in candles]
    highs = [x[2] for x in candles]
    lows = [x[3] for x in candles]
    closes = [x[4] for x in candles]
    volumes = [x[5] for x in candles]

    hist = macd_histogram(closes)
    rs = rsi(closes)
    k, d = stochastic(highs, lows, closes)

    i = len(candles) - 1
    p = i - 1
    if any(series[idx] is None for series in (hist, rs, k, d) for idx in (p, i)):
        raise RuntimeError('brak danych wskaźników')

    short_flip = hist[p] > 0 and hist[i] < 0
    long_flip = hist[p] < 0 and hist[i] > 0
    mc_available = current_mc is not None and previous_mc is not None and previous_mc > 0
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
    side = 'SHORT' if short_score >= long_score else 'LONG'
    score = max(short_score, long_score)
    selected_flip = short_flip if side == 'SHORT' else long_flip

    if short_flip:
        flip_label = 'MACD ZIELONY→CZERWONY'
    elif long_flip:
        flip_label = 'MACD CZERWONY→ZIELONY'
    else:
        flip_label = 'MACD bez świeżej zmiany'

    mc_change = None
    if mc_available:
        mc_change = (current_mc / previous_mc - 1) * 100

    return {
        'inst': inst,
        'side': side,
        'score': score,
        'exact': score == 8,
        'rsi': rs[i],
        'prev_k': k[p],
        'k': k[i],
        'd': d[i],
        'macd_flip': selected_flip,
        'flip_label': flip_label,
        'market_cap': current_mc,
        'market_cap_change': mc_change,
        'market_cap_available': mc_available,
    }


def format_market_cap(value):
    if value is None:
        return 'niedostępny'
    if value >= 1_000_000_000_000:
        return f'${value / 1_000_000_000_000:.2f}T'
    if value >= 1_000_000_000:
        return f'${value / 1_000_000_000:.2f}B'
    if value >= 1_000_000:
        return f'${value / 1_000_000:.1f}M'
    if value >= 1_000:
        return f'${value / 1_000:.1f}K'
    return f'${value:.0f}'


def format_market_cap_status(row):
    current = row.get('market_cap')
    change = row.get('market_cap_change')
    if current is None:
        return 'MC niedostępny'
    if change is None:
        return f"MC {format_market_cap(current)} | brak historii 1H"
    if change > 0:
        arrow = '↑'
    elif change < 0:
        arrow = '↓'
    else:
        arrow = '→'
    return f"MC {format_market_cap(current)} {arrow} {change:+.2f}%/1H"


def send_ntfy(message):
    response = requests.post(
        f'https://ntfy.sh/{NTFY_TOPIC}',
        data=message.encode('utf-8'),
        headers={'Title': 'BloFin 1H Scanner'},
        timeout=20,
    )
    response.raise_for_status()


def main():
    try:
        coins = get_universe()
        if not coins:
            raise RuntimeError('Nie znaleziono aktywnych USDT-M')

        previous_market_caps = load_marketcap_state()
        marketcap_error = None
        try:
            market_caps_by_symbol = fetch_market_caps()
        except Exception as exc:
            market_caps_by_symbol = {}
            marketcap_error = f'{type(exc).__name__}: {exc}'

        current_market_caps = {
            coin['inst']: market_cap_for_instrument(coin['inst'], market_caps_by_symbol)
            for coin in coins
        }

        results = []
        errors = []

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {
                executor.submit(
                    analyze,
                    coin['inst'],
                    current_market_caps.get(coin['inst']),
                    previous_market_caps.get(coin['inst']),
                ): coin
                for coin in coins
            }
            for future in as_completed(futures):
                coin = futures[future]
                try:
                    result = future.result()
                    result.update(coin)
                    results.append(result)
                except Exception as exc:
                    errors.append(f"{coin['inst']}: {type(exc).__name__}: {exc}")

        results.sort(
            key=lambda x: (x['macd_flip'], x['score'], -x['blofin_rank']),
            reverse=True,
        )

        exact = [x for x in results if x['exact']]
        chosen = exact if exact else results[:REPORT_N]
        flips = sum(1 for x in results if x['macd_flip'])

        if exact:
            header = f"Przeskanowano TOP {len(coins)} USDT-M na 1H. Świeże zmiany MACD: {flips}. PEŁNY SETUP 8/8 ({len(exact)}):"
        else:
            header = f"Przeskanowano TOP {len(coins)} USDT-M na 1H. Świeże zmiany MACD: {flips}. Najlepsze {min(REPORT_N, len(results))}:"

        lines = [
            f"{n}. {x['inst']} {x['side']} — {x['score']}/8 | {x['flip_label']} | BloFin #{x['blofin_rank']} {x['change']:+.2f}% | RSI {x['rsi']:.1f} | STOCH K {x['prev_k']:.1f}→{x['k']:.1f}, D {x['d']:.1f} | {format_market_cap_status(x)}"
            for n, x in enumerate(chosen, 1)
        ]

        message = header + '\n' + '\n'.join(lines)
        if marketcap_error:
            message += '\nMarket Cap chwilowo niedostępny — pozostałe 7 warunków policzono normalnie.'
            print(f'MARKET CAP ERROR: {marketcap_error}')
        elif not previous_market_caps:
            message += '\nMarket Cap: zapisano pierwszy punkt odniesienia. Porównanie 1H będzie dostępne od następnego pełnego skanu godzinowego.'

        if errors:
            message += f"\nPominięto {len(errors)} instrumentów."
            print('POMINIĘTE / BŁĘDY:')
            for error in errors:
                print(error)

        send_ntfy(message)
        print(message)

        if market_caps_by_symbol and should_save_hourly_state():
            state_values = {
                inst: value
                for inst, value in current_market_caps.items()
                if value is not None and value > 0
            }
            if state_values:
                save_marketcap_state(state_values)

    except Exception as exc:
        message = f"BŁĄD SKANERA: {type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)
        try:
            send_ntfy(message)
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
