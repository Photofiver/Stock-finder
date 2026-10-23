# 90-day BloFin winner-exclusive characteristics — research only
**LIVE unchanged. Historical strategy replay, not actual broker fills.**
Portfolio 500 USDT, 1x, <=5 USDT/position, up to 4: **1917 winners, 3858 losers, 5775 trades**.
All after 0.06% fees and assumed 0.025% adverse execution each side.
Train 60 days, validation 15 days, independent holdout 15 days. Train-derived thresholds.

Question: Is there a pre-entry condition all winners share and all losers lack?
Any zero-loss pattern found by looking at ALL 90 days is a hindsight description only; it cannot be assumed to recur.

## LONG
Base train/val/test/all90 [W,L]: {'train': (1035, 2058), 'validation': (247, 482), 'test': (215, 432), 'all90': (1497, 2972)}
Unique atomic filters 261, tested intersections: {'1': 261, '2': 30921, '3': 2117660}
Exclusively winners retrospectively on all90: 3010
Train winner-only (>=8): 35
TRAIN+VALIDATION winner-only (>=10W train and >=3W validation, no validation losses): 0
### Highest coverage hindsight zero-loss patterns (NOT validated)
- [['atr_pct', '<=', 0.3848231164], ['ma20_gap', '>=', 1.6102475372], ['obv5_strength', '<=', 14.8691514671]]: ALL90 [10, 0] [W,L], TRAIN [5, 0], VALID [2, 0], TEST [3, 0]
- [['ma10_gap', '>=', 1.1107959917], ['macd_pct', '<=', 0.1057578595], ['vol_strength', '>=', 1.1343283582]]: ALL90 [10, 0] [W,L], TRAIN [8, 0], VALID [1, 0], TEST [1, 0]
- [['rsi', '>=', 64.8127213422], ['stoch_k', '<=', 90.6574394464], ['width_pct', '>=', 1.3024282561]]: ALL90 [10, 0] [W,L], TRAIN [10, 0], VALID [0, 0], TEST [0, 0]
- [['atr_pct', '<=', 0.3848231164], ['ma20_gap', '>=', 1.6102475372], ['macd_delta_pct', '<=', 0.035782428]]: ALL90 [9, 0] [W,L], TRAIN [4, 0], VALID [2, 0], TEST [3, 0]
- [['atr_pct', '<=', 0.3848231164], ['ma20_gap', '>=', 1.6102475372], ['obv5_strength', '<=', 7.5810466381]]: ALL90 [9, 0] [W,L], TRAIN [4, 0], VALID [2, 0], TEST [3, 0]
- [['atr_pct', '<=', 0.3848231164], ['ma20_gap', '>=', 1.6102475372], ['vol_strength', '<=', 3.5584840413]]: ALL90 [9, 0] [W,L], TRAIN [4, 0], VALID [2, 0], TEST [3, 0]
- [['atr_pct', '<=', 0.3848231164], ['ma20_gap', '>=', 1.6102475372], ['width_pct', '>=', 0.5818786367]]: ALL90 [9, 0] [W,L], TRAIN [5, 0], VALID [1, 0], TEST [3, 0]
- [['ma10_gap', '<=', 0.2871531435], ['tp_sl_ratio', '<=', 0.5333333333], ['width_pct', '>=', 0.7515079601]]: ALL90 [9, 0] [W,L], TRAIN [5, 0], VALID [4, 0], TEST [0, 0]
### Validation-selected winner-only patterns, holdout measured without reranking
### Most favorable single predicates by validation wins-minus-losses (exploratory)
- width_pct >= 1.0631: TRAIN 183/128 VALID 25/18 TEST 10/12
- stoch_kd >= 26.667: TRAIN 79/77 VALID 23/18 TEST 8/14
- width_pct >= 1.3024: TRAIN 104/52 VALID 7/4 TEST 6/5
- tp_sl_ratio <= 0.53333: TRAIN 178/132 VALID 26/24 TEST 5/18
- macd_delta_pct >= 0.048116: TRAIN 88/68 VALID 12/10 TEST 6/11
- body_pct >= 0.85878: TRAIN 96/60 VALID 9/8 TEST 5/9
- width_pct >= 0.88694: TRAIN 312/308 VALID 52/51 TEST 35/36
- body_pct >= 0.66438: TRAIN 172/139 VALID 20/21 TEST 14/17

## SHORT
Base train/val/test/all90 [W,L]: {'train': (317, 713), 'validation': (51, 80), 'test': (52, 93), 'all90': (420, 886)}
Unique atomic filters 264, tested intersections: {'1': 264, '2': 30784, '3': 2008397, '4': 2923}
Exclusively winners retrospectively on all90: 6770
Train winner-only (>=8): 144
TRAIN+VALIDATION winner-only (>=10W train and >=3W validation, no validation losses): 10
### Highest coverage hindsight zero-loss patterns (NOT validated)
- [['body_pct', '>=', 0.6759751773], ['macd_delta_pct', '<=', -0.0147047042], ['tp_sl_ratio', '<=', 0.6332236842], ['vol_strength', '<=', 0.7364666704]]: ALL90 [16, 0] [W,L], TRAIN [13, 0], VALID [2, 0], TEST [1, 0]
- [['macd_delta_pct', '<=', -0.0147047042], ['macd_pct', '<=', -0.0515742586], ['tp_sl_ratio', '<=', 0.6332236842], ['width_pct', '>=', 1.783710117]]: ALL90 [15, 0] [W,L], TRAIN [13, 0], VALID [0, 0], TEST [2, 0]
- [['macd_delta_pct', '<=', -0.0147047042], ['macd_pct', '<=', -0.021639426], ['tp_sl_ratio', '<=', 0.6332236842], ['width_pct', '>=', 1.783710117]]: ALL90 [15, 0] [W,L], TRAIN [13, 0], VALID [0, 0], TEST [2, 0]
- [['body_pct', '>=', 0.4196642686], ['macd_pct', '<=', -0.0515742586], ['tp_sl_ratio', '<=', 0.6332236842], ['width_pct', '>=', 1.783710117]]: ALL90 [15, 0] [W,L], TRAIN [13, 0], VALID [0, 0], TEST [2, 0]
- [['body_pct', '>=', 0.6759751773], ['macd_pct', '<=', -0.0872799825], ['tp_sl_ratio', '<=', 0.6332236842], ['width_pct', '>=', 1.783710117]]: ALL90 [15, 0] [W,L], TRAIN [13, 0], VALID [0, 0], TEST [2, 0]
- [['body_pct', '>=', 0.6759751773], ['macd_pct', '<=', -0.0515742586], ['tp_sl_ratio', '<=', 0.6332236842], ['width_pct', '>=', 1.783710117]]: ALL90 [15, 0] [W,L], TRAIN [13, 0], VALID [0, 0], TEST [2, 0]
- [['body_pct', '>=', 0.6759751773], ['macd_pct', '<=', -0.021639426], ['tp_sl_ratio', '<=', 0.6332236842], ['width_pct', '>=', 1.783710117]]: ALL90 [15, 0] [W,L], TRAIN [13, 0], VALID [0, 0], TEST [2, 0]
- [['body_pct', '>=', 0.6759751773], ['macd_pct', '<=', 0.0], ['tp_sl_ratio', '<=', 0.6332236842], ['width_pct', '>=', 1.783710117]]: ALL90 [15, 0] [W,L], TRAIN [13, 0], VALID [0, 0], TEST [2, 0]
### Validation-selected winner-only patterns, holdout measured without reranking
- [['stoch_kd', '<=', -17.5779092702], ['tp_sl_ratio', '<=', 0.9090909091], ['vol_strength', '<=', 0.7364666704]]: TRAIN [14, 1], VALID [3, 0], HOLDOUT [2, 2], ALL90 [19, 3]
- [['stoch_kd', '<=', -17.5779092702], ['tp_sl_ratio', '<=', 0.9090909091], ['vol_strength', '<=', 0.7364666704], ['width_pct', '>=', 0.4738074957]]: TRAIN [14, 1], VALID [3, 0], HOLDOUT [2, 2], ALL90 [19, 3]
- [['stoch_kd', '<=', -17.5779092702], ['tp_sl_ratio', '<=', 0.9090909091], ['vol_strength', '<=', 0.7364666704], ['width_pct', '>=', 0.581781435]]: TRAIN [14, 1], VALID [3, 0], HOLDOUT [2, 2], ALL90 [19, 3]
- [['stoch_kd', '<=', -17.5779092702], ['tp_sl_ratio', '<=', 0.9090909091], ['vol_strength', '<=', 0.7364666704], ['width_pct', '>=', 0.7640067912]]: TRAIN [14, 1], VALID [3, 0], HOLDOUT [1, 2], ALL90 [18, 3]
- [['stoch_k', '<=', 10.3825136612], ['tp_sl_ratio', '<=', 0.6332236842], ['vol_ratio', '>=', 1.0673854447], ['vol_strength', '<=', 1.8961422428]]: TRAIN [14, 1], VALID [3, 0], HOLDOUT [2, 1], ALL90 [19, 2]
- [['stoch_k', '<=', 10.3825136612], ['stoch_kd', '<=', -5.6541353383], ['tp_sl_ratio', '<=', 0.6332236842], ['vol_strength', '<=', 1.8961422428]]: TRAIN [13, 1], VALID [3, 0], HOLDOUT [1, 0], ALL90 [17, 1]
### Most favorable single predicates by validation wins-minus-losses (exploratory)
- macd_pct <= -0.08728: TRAIN 111/250 VALID 26/15 TEST 14/19
- macd_pct <= -0.1564: TRAIN 69/137 VALID 13/5 TEST 2/9
- atr_pct >= 0.66614: TRAIN 135/227 VALID 14/6 TEST 8/12
- ma10_gap <= -0.76719: TRAIN 79/127 VALID 13/8 TEST 7/8
- ma20_gap <= -0.72625: TRAIN 129/232 VALID 24/19 TEST 13/18
- body_pct >= 0.67598: TRAIN 81/126 VALID 11/7 TEST 4/4
- tp_sl_ratio <= 0.90909: TRAIN 54/49 VALID 11/8 TEST 9/7
- macd_pct >= 0.1073: TRAIN 22/31 VALID 3/0 TEST 1/3

## Decision
Only a rule with meaningful sample size and no loss in independent holdout could be called validated within this specific dataset, and even that is not a guarantee of future profitability.
Do not enable winner-exclusive hindsight combinations in LIVE without forward paper testing.
