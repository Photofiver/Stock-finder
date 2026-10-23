# BloFin 90-day exit policy research (READ-ONLY)
Candidates: 5994 (middle scenario) | variants: 195
Baseline reproduction (all 90 days): {"20": {"observed_net": -2.050609, "expected_net": -2.050609, "difference": 0.0, "n": 288}, "100": {"observed_net": -10.0048992, "expected_net": -10.004899, "difference": -2e-07, "n": 1060}, "500": {"observed_net": -50.0370802, "expected_net": -50.03708, "difference": -2e-07, "n": 5775}, "1000": {"observed_net": -50.0370802, "expected_net": -50.03708, "difference": -2e-07, "n": 5775}}

## Baseline portfolio (middle execution cost 0.025% each way)
- train: 1060 trades, 325 W / 735 L, net -10.00490 USDT on 100 USDT
- validation: 860 trades, 298 W / 562 L, net -6.97505 USDT on 100 USDT
- test: 792 trades, 267 W / 525 L, net -5.93012 USDT on 100 USDT
- all90: 1060 trades, 325 W / 735 L, net -10.00490 USDT on 100 USDT

## Top variations chosen on training/validation, NEVER on independent test
1. tp1.2_sl0.2_h60: validation 831 trades, 174W/657L, -4.67364 USDT; TEST 777 trades, 172W/605L, -4.18101 USDT
2. tp0.8_sl0.2_h60: validation 841 trades, 200W/641L, -4.82142 USDT; TEST 786 trades, 182W/604L, -5.15065 USDT
3. tp1.2_sl0.2_h30_be0.35: validation 852 trades, 272W/580L, -4.91955 USDT; TEST 791 trades, 244W/547L, -4.33945 USDT
4. tp0.8_sl0.2_h30_be0.35: validation 853 trades, 272W/581L, -4.96351 USDT; TEST 793 trades, 244W/549L, -4.83527 USDT
5. tp0.6_sl0.2_h60: validation 848 trades, 232W/616L, -5.00323 USDT; TEST 790 trades, 208W/582L, -5.40030 USDT
6. tp1.2_sl0.2_h30_be0.2: validation 856 trades, 303W/553L, -5.03057 USDT; TEST 792 trades, 286W/506L, -4.06778 USDT
7. tp0.8_sl0.2_h30_be0.5: validation 853 trades, 244W/609L, -5.05050 USDT; TEST 792 trades, 217W/575L, -4.84588 USDT
8. tp1.2_sl0.35_h60: validation 808 trades, 229W/579L, -5.05865 USDT; TEST 752 trades, 241W/511L, -3.91368 USDT
9. tp0.8_sl0.2_h30_be0.2: validation 856 trades, 303W/553L, -5.06739 USDT; TEST 794 trades, 286W/508L, -4.54732 USDT
10. tp1.2_sl0.2_h30_be0.5: validation 852 trades, 244W/608L, -5.09160 USDT; TEST 789 trades, 217W/572L, -4.27259 USDT
11. tp1.2_sl0.2_h20_be0.2: validation 859 trades, 287W/572L, -5.15080 USDT; TEST 794 trades, 276W/518L, -4.24944 USDT
12. tp1.2_sl0.2_h20_be0.35: validation 858 trades, 256W/602L, -5.17110 USDT; TEST 793 trades, 235W/558L, -4.52368 USDT
13. tp0.8_sl0.35_h60: validation 825 trades, 264W/561L, -5.26574 USDT; TEST 764 trades, 255W/509L, -4.83872 USDT
14. tp1.2_sl0.2_h20_be0.5: validation 858 trades, 238W/620L, -5.28018 USDT; TEST 793 trades, 222W/571L, -4.48967 USDT
15. tp0.6_sl0.2_h30_be0.35: validation 856 trades, 273W/583L, -5.29101 USDT; TEST 793 trades, 244W/549L, -5.16165 USDT

## Limitations
5m OHLC cannot reproduce broker fills or unknown intra-bar order; cost model is estimated, not verified exact fills.
Not a recommendation for LIVE until independent robustness review.
