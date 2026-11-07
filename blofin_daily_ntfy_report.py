"""Read-only BloFin LIVE daily progress report for GitHub Actions -> ntfy."""
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

TARGET = Decimal('120000')
STATE_PATH = Path('blofin_live_state.json')
UK = ZoneInfo('Europe/London')


def amount(raw, name):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'Invalid {name}') from exc
    if not value.is_finite():
        raise ValueError(f'Non-finite {name}')
    return value


def fmt(value, digits=4):
    return f'{value:,.{digits}f}'.replace(',', ' ')


def rate_text(growth):
    return f'{growth * 100:+.4f}%/dzien'


def rate_from(capital_then, capital_now, days):
    if days <= 0 or capital_then <= 0 or capital_now <= 0:
        return None
    return math.expm1(math.log(float(capital_now / capital_then)) / days)


def verified_history(data):
    rows = []
    for row in data.get('trade_history', []):
        if not isinstance(row, dict):
            continue
        try:
            t = int(row.get('closed_ms') or 0)
            pnl = amount(row['net_pnl_usdt'], 'trade PnL')
        except (ValueError, TypeError, KeyError):
            continue
        if t > 0:
            rows.append((t, pnl, row))
    return sorted(rows, key=lambda item: item[0])


def reset_suffix(rows, net_since_reset):
    # Match a contiguous suffix of verified closed PnL to the capital reset.
    # If there is no unique match, do not invent a reset date.
    total = Decimal('0')
    matches = []
    for index in range(len(rows) - 1, -1, -1):
        total += rows[index][1]
        if abs(total - net_since_reset) <= Decimal('0.002'):
            matches.append(index)
    return rows[matches[0]:] if len(matches) == 1 else None


def report(data, now):
    if not isinstance(data, dict):
        raise ValueError('Invalid LIVE state')
    start = amount(data.get('bankroll_start_usdt', '20'), 'bankroll_start_usdt')
    realized = amount(data.get('realized_pnl_usdt', '0'), 'realized_pnl_usdt')
    baseline = amount(data.get('bankroll_pnl_baseline_usdt', '0'), 'bankroll_pnl_baseline_usdt')
    bonus = amount(data.get('bankroll_streak_bonus_usdt', '0'), 'bankroll_streak_bonus_usdt')
    earned = realized - baseline
    current = start + earned  # streak bonuses are allocation, not trading profit
    if start <= 0 or current <= 0:
        raise ValueError('Nonpositive strategy capital; cannot report percentage return')
    rows = verified_history(data)
    since_reset = reset_suffix(rows, earned)
    cutoff24 = int((now - timedelta(hours=24)).timestamp() * 1000)
    recent = [row for row in rows if row[0] >= cutoff24]
    pnl24 = sum((row[1] for row in recent), Decimal('0'))
    w24 = sum(row[1] > 0 for row in recent)
    l24 = sum(row[1] < 0 for row in recent)
    stats = since_reset if since_reset is not None else rows
    wins = sum(row[1] > 0 for row in stats)
    losses = sum(row[1] < 0 for row in stats)
    count = wins + losses
    accuracy = (wins / count * 100) if count else 0

    positions = data.get('positions') or {}
    if isinstance(positions, dict):
        opened = [f"{p.get('inst', inst)} {p.get('side', '?')}" for inst, p in positions.items()
                  if isinstance(p, dict) and p.get('inst')]
    else:
        opened = []
    last_ms = int(data.get('last_run_ms') or 0)
    last = datetime.fromtimestamp(last_ms / 1000, tz=UK) if last_ms else None
    last_txt = last.strftime('%d.%m %H:%M') if last else 'brak'
    age = (now - last).total_seconds() / 60 if last else float('inf')
    scanner = 'aktywny' if -5 <= age <= 30 else 'brak swiezego potwierdzenia'
    now_uk = now.astimezone(UK)
    lines = [
        f"BloFin LIVE - {now_uk.strftime('%d.%m.%Y %H:%M')} UK",
        f'Cel: {fmt(TARGET, 0)} USDT',
        f'Kapital netto: {fmt(current)} USDT (start {fmt(start)} + wynik {fmt(earned, 4)})',
        f'Premie alokacji (NIE zysk): {fmt(bonus)} USDT',
        f'Wynik netto 24h: {pnl24:+.4f} USDT (W{w24}/L{l24})',
        f'Zamkniete transakcje {"od resetu" if since_reset is not None else "w historii"}: W{wins}/L{losses}; skutecznosc {accuracy:.2f}%',
        f'Otwarte pozycje ({len(opened)}): {", ".join(opened[:4]) if opened else "brak"}',
        f'Skaner: {scanner}; ostatni skan {last_txt} UK',
    ]

    if current >= TARGET:
        lines.append('Pozostalo do celu: 0 dni (cel osiagniety)')
    elif since_reset is None or not since_reset:
        lines.append('Pozostalo do celu: brak wiarygodnej prognozy liczby dni (nie da sie potwierdzic okresu po resecie)')
    else:
        first = since_reset[0]
        opened_ms = int(first[2].get('opened_ms') or first[0])
        # Use the first verified trade after reset as a measurable sample start.
        sample_days = (now.timestamp() * 1000 - opened_ms) / 86400000
        daily = rate_from(start, current, sample_days)
        if sample_days < 7 or daily is None or daily <= 0:
            lines.append('Pozostalo do celu: brak wiarygodnej prognozy liczby dni')
            lines.append(f'Proba: {sample_days:.2f} dni; wzrost: {rate_text(daily) if daily is not None else "brak"}')
        else:
            days_to_go = math.ceil(math.log(float(TARGET / current)) / math.log1p(daily))
            lines.append(f'Pozostalo do celu: {days_to_go} dni (PROGNOZA, nie gwarancja)')
            lines.append(f'Tempo empiryczne: {rate_text(daily)}; proba {sample_days:.2f} dni')
        for days in (7, 30):
            boundary = int((now - timedelta(days=days)).timestamp() * 1000)
            if opened_ms > boundary:
                lines.append(f'Srednia {days} dni: brak pelnej probki')
                continue
            pnl = sum((r[1] for r in since_reset if r[0] >= boundary), Decimal('0'))
            old_capital = current - pnl
            growth = rate_from(old_capital, current, days)
            lines.append(f'Srednia {days} dni: {rate_text(growth) if growth is not None else "brak danych"}')

    lines.append('Zrodlo: github.com/Photofiver/Stock-finder/blob/main/blofin_live_state.json')
    return '\n'.join(lines)


def main():
    data = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    print(report(data, datetime.now(timezone.utc)))


if __name__ == '__main__':
    main()
