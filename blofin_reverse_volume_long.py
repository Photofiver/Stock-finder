"""Reverse-volume LONG signal engine and offline replay. No exchange orders.

Rows: {5m_open_timestamp_ms: (open, high, low, close, volume)}.
Only CLOSED bars may be supplied to entry_signal/update_exit.
Replay fills at the following bar open; these prices are simulation assumptions.
"""
from dataclasses import dataclass
import math

STEP = 300_000
STRATEGY = 'REVERSE_VOLUME_LONG'


def valid_bar(bar):
    return (bar is not None and len(bar) == 5
            and all(math.isfinite(float(x)) for x in bar)
            and min(bar[:4]) > 0 and bar[4] >= 0
            and bar[1] >= max(bar[0], bar[3])
            and bar[2] <= min(bar[0], bar[3]))


def mean10(rows, ts):
    previous = [rows.get(ts - j * STEP) for j in range(1, 11)]
    if not all(valid_bar(x) for x in previous):
        return None
    return sum(x[4] for x in previous) / 10


def entry_signal(rows, btc, ts, rank):
    """Evaluate the frozen rule, with no +DI, RSI, 1h or engulfing filter."""
    if not isinstance(rank, int) or not 1 <= rank <= 200:
        return False
    cur, prev, old = (rows.get(t) for t in (ts, ts-STEP, ts-30*STEP))
    if not all(valid_bar(x) for x in (cur, prev, old)):
        return False
    avg = mean10(rows, ts)
    if avg is None or avg <= 0:
        return False
    if not (cur[3] < cur[0] and prev[3] > prev[0] and prev[4] > 0
            and 3 <= cur[4] / prev[4] < 4 and cur[4] <= avg
            and cur[3] >= old[3]):
        return False
    # Reject only an entirely inside bar, not all non-engulfing candles.
    if cur[1] <= prev[1] and cur[2] >= prev[2]:
        return False
    now_btc, old_btc = btc.get(ts), btc.get(ts-3*STEP)
    if not all(valid_bar(x) for x in (now_btc, old_btc)):
        return False
    if 100 * (now_btc[3] / old_btc[3] - 1) > .062:
        return False
    last15 = ((ts+STEP)//(3*STEP)-1)*3*STEP+2*STEP
    # Check the whole completed 15m candle and its preceding close.
    bars15 = [rows.get(last15-j*STEP) for j in range(4)]
    if not all(valid_bar(x) for x in bars15):
        return False
    return 100 * (bars15[0][3] / bars15[3][3] - 1) >= -.103


@dataclass
class Position:
    entry_ms: int
    entry_price: float
    last_red_volume: float
    last_processed_ms: int


def update_exit(position, rows, ts):
    """Return HOLD/EXIT/GAP. EXIT fills at ts+STEP, never within signal bar."""
    if ts <= position.last_processed_ms:
        return 'HOLD'
    if ts != position.last_processed_ms + STEP:
        return 'GAP'
    cur = rows.get(ts)
    avg = mean10(rows, ts)
    if not valid_bar(cur) or avg is None:
        return 'GAP'
    position.last_processed_ms = ts
    if cur[4] > avg:
        return 'HOLD'
    if cur[3] < cur[0]:
        position.last_red_volume = cur[4]
    elif cur[3] > cur[0] and cur[4] > position.last_red_volume:
        return 'EXIT'
    return 'HOLD'


def replay(data, start_ms, end_ms):
    """Independent paper positions per coin; gross percentages, no fees.

    TOP200 is reconstructed from signal close / close 24h earlier.
    Missing history is not imputed. Unresolved positions are not counted as wins.
    """
    if start_ms % STEP or end_ms % STEP or start_ms >= end_ms:
        raise ValueError('Use ordered 5-minute-aligned boundaries')
    btc = data.get('BTC-USDT')
    if not btc:
        raise ValueError('BTC-USDT history required')
    ranks = {}
    coverage = []
    for ts in range(start_ms-STEP, end_ms, STEP):
        prices = []
        for coin, rows in data.items():
            now, old = rows.get(ts), rows.get(ts-288*STEP)
            if valid_bar(now) and valid_bar(old):
                prices.append((now[3]/old[3]-1, coin))
        if len(prices) < 200:
            raise ValueError(f'Insufficient historical ranking at {ts}')
        prices.sort(reverse=True)
        ranks[ts] = {coin: rank for rank, (_, coin) in enumerate(prices[:200], 1)}
        coverage.append(len(prices))
    closed, unresolved = [], []
    for coin, rows in data.items():
        position = None
        for ts in range(start_ms-STEP, end_ms, STEP):
            if position:
                decision = update_exit(position, rows, ts)
                if decision == 'GAP':
                    unresolved.append({'coin': coin, 'entry_ms': position.entry_ms, 'reason': 'GAP'})
                    position = None
                elif decision == 'EXIT':
                    nxt = rows.get(ts+STEP)
                    if valid_bar(nxt):
                        closed.append({'coin': coin, 'side': 'LONG',
                            'entry_ms': position.entry_ms, 'exit_ms': ts+STEP,
                            'entry_price': position.entry_price, 'exit_price': nxt[0],
                            'gross_pct': 100*(nxt[0]/position.entry_price-1)})
                    else:
                        unresolved.append({'coin': coin, 'entry_ms': position.entry_ms, 'reason': 'EXIT_PRICE_MISSING'})
                    position = None
            if position is None and start_ms <= ts+STEP < end_ms:
                if entry_signal(rows, btc, ts, ranks[ts].get(coin)):
                    nxt = rows.get(ts+STEP)
                    if valid_bar(nxt):
                        position = Position(ts+STEP, nxt[0], rows[ts][4], ts)
        if position:
            unresolved.append({'coin': coin, 'entry_ms': position.entry_ms, 'reason': 'OPEN_AT_END'})
    return {'strategy': STRATEGY, 'mode': 'OFFLINE_REPLAY',
            'start_ms': start_ms, 'end_ms': end_ms,
            'rank_coverage_min': min(coverage), 'closed': len(closed),
            'wins': sum(t['gross_pct'] > 0 for t in closed),
            'losses': sum(t['gross_pct'] < 0 for t in closed),
            'flat': sum(t['gross_pct'] == 0 for t in closed),
            'gross_sum_percentage_points': sum(t['gross_pct'] for t in closed),
            'trades': closed, 'unresolved': unresolved}


def main():
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='JSON with data, start_ms and end_ms')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text())
    data = {coin: {int(ts): tuple(bar) for ts, bar in rows.items()}
            for coin, rows in payload['data'].items()}
    result = replay(data, int(payload['start_ms']), int(payload['end_ms']))
    Path(args.output).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'trades'}))


if __name__ == '__main__':
    main()
