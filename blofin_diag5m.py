from datetime import datetime, timezone
import os

os.environ["TIMEFRAME"] = "5m"
import blofin_scanner as s

coins, ranking_errors = s.get_universe()
print(f"TOP10 count={len(coins)}")
for coin in coins:
    candles = s.get_closed_candles(coin["inst"])
    closes = [row[4] for row in candles]
    volumes = [row[5] for row in candles]
    hist = s.macd_histogram(closes)
    i = len(candles) - 1
    p = i - 1
    hp = hist[p]
    hi = hist[i]
    short_flip = hp is not None and hi is not None and hp > 0 and hi < 0
    long_flip = hp is not None and hi is not None and hp < 0 and hi > 0
    vol_up = volumes[i] > volumes[p]
    side = "SHORT" if short_flip else "LONG" if long_flip else "NONE"
    tp = datetime.fromtimestamp(candles[p][0] / 1000, tz=timezone.utc).isoformat()
    ti = datetime.fromtimestamp(candles[i][0] / 1000, tz=timezone.utc).isoformat()
    print(
        f"#{coin['blofin_rank']} {coin['inst']} 24h={coin['change']:+.2f}% | "
        f"candles {tp}->{ti} | hist {hp:+.8f}->{hi:+.8f} flip={side} | "
        f"vol {volumes[p]:.4f}->{volumes[i]:.4f} up={vol_up} | "
        f"score={(1 if (short_flip or long_flip) else 0) + (1 if vol_up else 0)}/2"
    )
if ranking_errors:
    print("ranking_errors:")
    for e in ranking_errors:
        print(e)
