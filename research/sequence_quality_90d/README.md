# 90d candle sequence + width/body/ATR entry-filter study
RESEARCH ONLY: LIVE unchanged; no new market candle downloads.
Reused frozen 5m candidate entries: 5994; candidates missing trailing sequence: 0.
Data: 5,142,770 5m candles for 200 BloFin coins, GitHub Actions run 39994070142.
Backtest cost: 0.06% fee + 0.025% modeled execution, per side.
Train 60d -> validation 15d -> holdout test 15d, chronologically.

## Baseline (100 USDT, 5 USDT each position)
- train: 1060 trades, 325W/735L, -10.00490 USDT
- validation: 860 trades, 298W/562L, -6.97505 USDT
- test: 792 trades, 267W/525L, -5.93012 USDT
- all90: 1060 trades, 325W/735L, -10.00490 USDT

## LONG — 984 candidate combinations
Train finalists 243; passed positive TRAIN+VALIDATION 0.
**No rule achieved positive train + validation under minimum sample requirements.**
1. {'width': 25, 'body': 75, 'atr': 25} + exact_6_DUUDDU: validation 8 (6W/2L), +0.01793; HOLDOUT 6 (3W/3L), -0.03937 USDT
2. {'width': 50, 'body': 25, 'atr': 50} + exact_3_XDU: validation 8 (6W/2L), +0.00785; HOLDOUT 5 (4W/1L), +0.02849 USDT
3. {'width': 75, 'body': 50, 'atr': 50} + exact_5_DUUDU: validation 8 (5W/3L), -0.00301; HOLDOUT 6 (4W/2L), -0.00873 USDT
4. {'width': 75, 'body': 50, 'atr': 25} + exact_5_DUUDU: validation 8 (5W/3L), -0.00301; HOLDOUT 9 (6W/3L), -0.02496 USDT
5. {'width': 75, 'body': 25, 'atr': 50} + exact_5_DUUDU: validation 8 (5W/3L), -0.00301; HOLDOUT 7 (5W/2L), +0.00794 USDT

## SHORT — 302 candidate combinations
Train finalists 202; passed positive TRAIN+VALIDATION 1.
SELECTED BEFORE HOLDOUT: {"side": "SHORT", "thresholds": {"width_pct": 0.8501689266310642, "body_pct": 0.5737344032235636, "atr_pct": 0.5211590772924151}, "quartiles": {"width": 25, "body": 75, "atr": 50}, "pattern": "breakout3"}
1. {'width': 25, 'body': 25, 'atr': 25} + exact_5_DUDDU: validation 8 (7W/1L), +0.19592; HOLDOUT 4 (3W/1L), +0.03679 USDT
2. {'width': 25, 'body': 75, 'atr': 50} + same_6_3: validation 8 (6W/2L), +0.12480; HOLDOUT 3 (1W/2L), -0.01027 USDT
3. {'width': 25, 'body': 75, 'atr': 25} + same_6_3: validation 10 (7W/3L), +0.11543; HOLDOUT 4 (1W/3L), -0.04110 USDT
4. {'width': 50, 'body': 75, 'atr': 25} + same_6_3: validation 8 (5W/3L), +0.10672; HOLDOUT 2 (1W/1L), +0.02001 USDT
5. {'width': 25, 'body': 75, 'atr': 50} + same_5_2: validation 8 (5W/3L), +0.10061; HOLDOUT 3 (2W/1L), +0.04938 USDT

## Only independently VAL-positive filters combined (no LIVE)
- capital 20, validation: 11 entries, 7W/4L, +0.00018 USDT (+0.001% of capital)
- capital 20, test: 6 entries, 3W/3L, -0.05682 USDT (-0.284% of capital)
- capital 20, all90: 98 entries, 54W/44L, -0.05035 USDT (-0.252% of capital)
- capital 100, validation: 11 entries, 7W/4L, +0.00018 USDT (+0.000% of capital)
- capital 100, test: 6 entries, 3W/3L, -0.05682 USDT (-0.057% of capital)
- capital 100, all90: 99 entries, 55W/44L, -0.04983 USDT (-0.050% of capital)
- capital 500, validation: 11 entries, 7W/4L, +0.00018 USDT (+0.000% of capital)
- capital 500, test: 6 entries, 3W/3L, -0.05682 USDT (-0.011% of capital)
- capital 500, all90: 99 entries, 55W/44L, -0.04983 USDT (-0.010% of capital)
- capital 1000, validation: 11 entries, 7W/4L, +0.00018 USDT (+0.000% of capital)
- capital 1000, test: 6 entries, 3W/3L, -0.05682 USDT (-0.006% of capital)
- capital 1000, all90: 99 entries, 55W/44L, -0.04983 USDT (-0.005% of capital)

## Interpretation
Repeated pre-entry patterns are observable, but a profitable filter requires both positive validation and holdout.
Inspect original Work replay limitations, unknown real spread and historical Top200; do NOT enable in LIVE from this study.
