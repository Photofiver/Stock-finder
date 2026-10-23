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
3. tp0.6_sl0.2_h60: validation 848 trades, 232W/616L, -5.00323 USDT; TEST 790 trades, 208W/582L, -5.40030 USDT
4. tp1.2_sl0.2_h30_be0.35: validation 852 trades, 162W/690L, -5.00462 USDT; TEST 792 trades, 159W/633L, -4.46518 USDT
5. tp1.2_sl0.35_h60: validation 808 trades, 229W/579L, -5.05865 USDT; TEST 752 trades, 241W/511L, -3.91368 USDT
6. tp0.8_sl0.2_h30_be0.5: validation 853 trades, 198W/655L, -5.05998 USDT; TEST 792 trades, 183W/609L, -4.93725 USDT
7. tp0.8_sl0.2_h30_be0.35: validation 854 trades, 178W/676L, -5.07052 USDT; TEST 793 trades, 165W/628L, -4.95036 USDT
8. tp1.2_sl0.2_h30_be0.5: validation 851 trades, 180W/671L, -5.08176 USDT; TEST 790 trades, 177W/613L, -4.41966 USDT
9. tp1.2_sl0.2_h20_be0.35: validation 858 trades, 188W/670L, -5.21627 USDT; TEST 793 trades, 185W/608L, -4.67783 USDT
10. tp0.8_sl0.35_h60: validation 825 trades, 264W/561L, -5.26574 USDT; TEST 764 trades, 255W/509L, -4.83872 USDT
11. tp1.2_sl0.2_h30_be0.2: validation 858 trades, 124W/734L, -5.26890 USDT; TEST 793 trades, 129W/664L, -4.34921 USDT
12. tp1.2_sl0.2_h20_be0.5: validation 858 trades, 199W/659L, -5.28470 USDT; TEST 793 trades, 200W/593L, -4.62675 USDT
13. tp0.8_sl0.2_h30_be0.2: validation 858 trades, 139W/719L, -5.31792 USDT; TEST 794 trades, 134W/660L, -4.83791 USDT
14. tp0.8_sl0.2_h30: validation 852 trades, 208W/644L, -5.32475 USDT; TEST 792 trades, 194W/598L, -5.01670 USDT
15. tp0.6_sl0.2_h30_be0.35: validation 857 trades, 202W/655L, -5.33626 USDT; TEST 793 trades, 180W/613L, -5.26661 USDT

## Limitations
5m OHLC cannot reproduce broker fills or unknown intra-bar order; cost model is estimated, not verified exact fills.
Not a recommendation for LIVE until independent robustness review.
