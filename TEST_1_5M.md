# TEST 1 — BloFin 5m Volume

Start: 2026-09-20, first intended 5m close 15:20 UK.

## Schedule — first hour
15:20, 15:25, 15:30, 15:35, 15:40, 15:45, 15:50, 15:55, 16:00, 16:05, 16:10, 16:15 UK.

GitHub schedule: every 5 minutes (`*/5 * * * *`). GitHub may start a scheduled job a little late; the bot only uses confirmed BloFin 5m candles.

## Strategy under test
- Market source: BloFin.
- Universe: current TOP10 linear USDT swaps selected by the bot's existing 24h-change ranking.
- Timeframe: native confirmed 5-minute candles.
- Indicators: only candle colour + Volume.
- Volume comparison:
  - compare the latest GREEN candle Volume with the latest RED candle Volume, regardless of how many candles are between them;
  - latest GREEN Volume > latest RED Volume => SHORT;
  - latest RED Volume > latest GREEN Volume => LONG;
  - exact equality => no new entry.
- Exit: opposite Volume flip, or exchange protection.
- TP: +1%.
- SL: -0.5%.
- Leverage: 1x.
- Margin: isolated.
- Maximum simultaneous positions: 4.
- New entries split the remaining bot bankroll equally across available slots/candidates.

## Risk stop
- TEST 1 starting bankroll: 10.00 USDT.
- Maximum cumulative drawdown: 10%.
- Hard-stop threshold: 9.00 USDT.
- At or below 9.00 USDT, all new entries are blocked.
- Existing open positions remain protected by their TP/SL and can still close normally.

## What is checked each run
- latest confirmed 5m candle from BloFin;
- candle colours and Volume;
- generated LONG/SHORT signal, if any;
- orders actually executed;
- position closures and realized PnL;
- TEST 1 bankroll and whether the 10% hard stop was triggered.
