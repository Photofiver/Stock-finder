# BloFin LIVE — test kapitału 20/100/500/1000 USDT

**Werdykt: nie wdrażać znalezionych filtrów. Brak potwierdzonej przewagi na walidacji. LIVE nie zmieniono.**

To replay obecnych reguł na danych OHLCV, nie rekonstrukcja rzeczywistych wykonań. Wynik netto oznacza wynik po prowizji i ZAŁOŻONYM koszcie wykonania, **bez historycznego funding**, którego nie ma w archiwach. Nie jest to wynik po wszystkich rzeczywistych kosztach.

## Źródła i zakres
- Kod: `afcd4a4fec111a01ce31ba511973fa06ffda2e6e`; aktywny `.github/workflows/blofin_live_signal_watch.yml`, potwierdzony uruchomieniem 39997625783.
- Dane: run 39994070142, wszystkie 10 shardów + universe; istniejące archiwa, brak nowych zapytań o świece.
- Odczytano 5 142 770 świec / 200 coinów, 196 pełnych historii; NES, DOS, CNPY i STONK krótsze.
- Parametry kontraktów pobrano z publicznego `/api/v1/market/instruments`: minSize, lotSize, contractValue, tickSize. Są to **obecne**, a nie historyczne specyfikacje; nie ma archiwum ich zmian.
- Odnaleziono 33 674 surowe sygnały tunelu/odwrócenia wolumenu. Po bramkach ceny, TP/SL i wielkości: 19 005 scenariuszowych kandydatów, nie 19 005 faktycznych transakcji. Ten sam sygnał może występować w kilku scenariuszach.
- 845 zapisanych rzeczywistych transakcji, 335 z kompletnymi wskaźnikami. Mediana opłaty wyliczona z 745 rekordów wynosi 0,06% na stronę. Dane rzeczywiste obejmują różne wersje strategii, więc nie mieszano ich wyników z kontrolowanym replayem.

## Podział czasu (UTC; w UK w tym okresie +1 h)
- Optymalizacja: [25.07.2026 17:15, 23.09.2026 17:15).
- Walidacja: [23.09.2026 17:15, 08.10.2026 17:15).
- Test: [08.10.2026 17:15, 23.10.2026 17:15).
Każdy odcinek portfelowy zaczyna się od podanego kapitału i pustego portfela; wcześniejsze świece służą tylko rozgrzaniu wskaźników. Osobno jest ciągły replay 90 dni. Trening odrzuca wyniki transakcji przekraczających granicę treningu. Symulacja portfela nie odrzuca wejść na podstawie przyszłego czasu wyjścia.

## Odtworzone reguły
- 5m; 48 zamkniętych świec do tunelu, cztery różne potwierdzone punkty; LONG drugie dolne, SHORT drugie górne odbicie.
- Zmiana koloru świecy i większy wolumen niż poprzednia świeca; pierwszeństwo volume / poprzednia średnia20.
- LONG: górna linia nie dalej niż 0,60%, dolna co najmniej 0,10% od ceny wejścia; poprawne zapisane wskaźniki. MA10 nie filtruje wejścia.
- SHORT: niekorzystna różnica ceny wykonania względem close co najmniej 0,05%. Użyto modelowanej ceny następnego open; rzeczywistego bid nie ma. To szczególnie ogranicza wiarygodność SHORT.
- TP 0,05% przed linią; musi być ponad szacunkowym buforem prowizji 0,12%. W trybie EARLY TP nie oddala się od wejścia. SL tylko zmniejsza ryzyko. Użyto bezpośrednio funkcji obecnego kodu do cen, przesuwania ochrony, drugiego odbicia, wskaźników i wielkości kontraktów.
- Wejście na następnym open. Zlecenia ochronne działają podczas następnej świecy; zmiany SL/TP dopiero po close, bez zastosowania do wcześniejszego high/low.
- Gdy świeca dotyka TP i SL, przyjęto SL. Luka przez SL daje gorszy open. Brak premii za korzystną lukę przez TP. To konserwatywna konwencja, nie znana kolejność transakcji.
- 1×, do 4 pozycji, maks. 5 USDT na pozycję, wymóg min. 5 USDT wolnego budżetu przed zaokrągleniem kontraktów, bufor gotówki 0,50 USDT. Kwota zlecenia po zaokrągleniu może być niższa niż 5 USDT.
- Zmieniono w symulacji wyłącznie całkowity kapitał/cap na 20/100/500/1000. Nie zwiększano limitu 5 USDT na pozycję.
- Próg strat 10% jest narastający, z zatrzaśnięciem, bez automatycznego codziennego resetu. Otwarte pozycje nadal mogą zmienić wynik po jego aktywacji.
- Ponowne wejście w coin wymaga nowych punktów po zamknięciu poprzedniej pozycji. Portfel nie sumuje niezależnych jednoczesnych inwestycji po pełnym kapitale.

## Koszty
Prowizja 0,06% wartości rzeczywistego modelowanego wejścia + 0,06% wartości wyjścia. Potwierdzenie ogólnej taryfy: https://blofin.com/en/support/FAQ/Top-Questions/9049331029775-How-Are-Futures-Trading-Fees-Calculated
Trzy scenariusze łącznego spreadu/poślizgu: 0%, 0,025% lub 0,05% niekorzystnego wykonania **na stronę**. Nie są to odtworzone dane rynkowe. Zmiana ceny wejścia może zmienić kwalifikację sygnału. W obrębie każdego scenariusza wszystkie kapitały dostają identyczną listę kandydatów.
Brutto w tabelach jest przed modelowanym kosztem wykonania; netto = brutto − prowizje − koszt wykonania. Procenty odnoszą się do całego kapitału startowego, nie sumy zwrotów pozycji.

## Obecna strategia: pełne 90 dni
Scenariusz środkowy: 0,025% na stronę, LONG+SHORT we wspólnym portfelu.

| Kapitał | Transakcje | Zyskowne | Stratne | Brutto USDT | Prowizje USDT | Wykonanie USDT | Netto USDT | Netto % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20 | 288 | 93 | 195 | 0.274635 | 1.641349 | 0.683895 | -2.050609 | -10.253045 |
| 100 | 1060 | 325 | 735 | -1.387699 | 6.082730 | 2.534471 | -10.004899 | -10.004899 |
| 500 | 5775 | 1917 | 3858 | -3.700516 | 32.708163 | 13.628401 | -50.037080 | -10.007416 |
| 1000 | 5775 | 1917 | 3858 | -3.700516 | 32.708163 | 13.628401 | -50.037080 | -5.003708 |

## Obecna strategia: niezależne ostatnie 15 dni

| Kapitał | Transakcje | Zyskowne | Stratne | Brutto USDT | Prowizje USDT | Wykonanie USDT | Netto USDT | Netto % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20 | 277 | 94 | 183 | 0.188855 | 1.536746 | 0.640311 | -1.988202 | -9.941010 |
| 100 | 792 | 267 | 525 | 0.348114 | 4.431698 | 1.846541 | -5.930125 | -5.930125 |
| 500 | 792 | 267 | 525 | 0.348114 | 4.431698 | 1.846541 | -5.930125 | -1.186025 |
| 1000 | 792 | 267 | 525 | 0.348114 | 4.431698 | 1.846541 | -5.930125 | -0.593012 |

## LONG i SHORT osobno — ostatnie 15 dni, kapitał 100 USDT

| Kierunek | Transakcje | Zyskowne | Stratne | Brutto USDT | Prowizje USDT | Wykonanie USDT | Netto USDT | Netto % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| LONG | 649 | 215 | 434 | 0.071547 | 3.645051 | 1.518771 | -5.092275 | -5.092275 |
| SHORT | 147 | 53 | 94 | 0.266167 | 0.809403 | 0.337251 | -0.880488 | -0.880488 |

## Wrażliwość na koszty: całe 90 dni

| Koszt/stronę % | Kapitał | Transakcje | Zyskowne | Stratne | Brutto USDT | Prowizje USDT | Wykonanie USDT | Netto USDT | Netto % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 20 | 369 | 145 | 224 | 0.092025 | 2.106493 | 0.000000 | -2.014468 | -10.072338 |
| 0 | 100 | 1297 | 487 | 810 | -2.579867 | 7.427134 | 0.000000 | -10.007001 | -10.007001 |
| 0 | 500 | 5160 | 2024 | 3136 | -4.329916 | 29.265886 | 0.000000 | -33.595802 | -6.719160 |
| 0 | 1000 | 5160 | 2024 | 3136 | -4.329916 | 29.265886 | 0.000000 | -33.595802 | -3.359580 |
| 0.025000 | 20 | 288 | 93 | 195 | 0.274635 | 1.641349 | 0.683895 | -2.050609 | -10.253045 |
| 0.025000 | 100 | 1060 | 325 | 735 | -1.387699 | 6.082730 | 2.534471 | -10.004899 | -10.004899 |
| 0.025000 | 500 | 5775 | 1917 | 3858 | -3.700516 | 32.708163 | 13.628401 | -50.037080 | -10.007416 |
| 0.025000 | 1000 | 5775 | 1917 | 3858 | -3.700516 | 32.708163 | 13.628401 | -50.037080 | -5.003708 |
| 0.050000 | 20 | 162 | 33 | 129 | -0.316075 | 0.924478 | 0.770398 | -2.010951 | -10.054754 |
| 0.050000 | 100 | 905 | 220 | 685 | -0.487833 | 5.191021 | 4.325851 | -10.004706 | -10.004706 |
| 0.050000 | 500 | 4556 | 1221 | 3335 | -2.399487 | 25.971501 | 21.642917 | -50.013905 | -10.002781 |
| 0.050000 | 1000 | 7321 | 1998 | 5323 | -1.677077 | 41.415311 | 34.512759 | -77.605148 | -7.760515 |

## Układy i selekcja filtrów
14 cech: RSI14, Stoch K, K−D, ATR14%, volume/MA20, volume/poprzedni volume, korpus%, szerokość tunelu%, odległości od SMA10 i SMA20, MACD histogram%, zmiana histogramu%, OBV5/MA20 volume i stosunek odległości TP/SL. Każda cecha: kwartyle z treningu, <= lub >=. Kombinacje do 4 różnych cech, beam search 40, minimum 30 kandydatów treningowych. To **ograniczone przeszukiwanie**, nie wszystkie możliwe wskaźniki/progi/kombinacje.

| Kierunek | Sprawdzone rozszerzenia | Unikalne maski | Finaliści walidacji |
| --- | --- | --- | --- |
| LONG | 3822 | 2247 | 40 |
| SHORT | 3960 | 2628 | 40 |

Wybrano jedną regułę na kierunek według walidacyjnego NET przy 100 USDT i >=5 zamknięć; progi zamrożono przed raportowaniem testu. **Obie wybrane reguły tracą na walidacji, więc żadna nie kwalifikuje się do LIVE.**

### LONG: wybrany kandydat badawczy, nie rekomendacja
ma20_gap >= 0.76449852773 AND macd_pct <= 0.0482870218465 AND rsi <= 63.2867688878 AND width_pct >= 0.529489060247

### SHORT: wybrany kandydat badawczy, nie rekomendacja
ma20_gap >= -0.146760199892 AND obv5_strength >= 0.0294585443394 AND stoch_kd <= -1.5191586237 AND width_pct >= 1.33397652356

### Pięć najwyżej sklasyfikowanych kandydatów na każdy kierunek
Kolejność z walidacji; bez sortowania po niezależnym teście. Wiersze ALL90 zawierają trening i nie są dowodem skuteczności. Różne progi mogą przepuścić te same transakcje.

#### all90: 100 USDT, środkowy koszt

| Kierunek | Ranking walidacji | Wystąpienia | Transakcje | Zyskowne | Stratne | Brutto USDT | Prowizje USDT | Wykonanie USDT | Netto USDT | Netto % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| LONG | 1 | 35 | 35 | 26 | 9 | 0.312035 | 0.199550 | 0.083146 | 0.029340 | 0.029340 |
| LONG | 2 | 35 | 35 | 26 | 9 | 0.305965 | 0.201170 | 0.083821 | 0.020974 | 0.020974 |
| LONG | 3 | 38 | 38 | 28 | 10 | 0.307215 | 0.215322 | 0.089717 | 0.002176 | 0.002176 |
| LONG | 4 | 39 | 39 | 28 | 11 | 0.315215 | 0.221305 | 0.092210 | 0.001700 | 0.001700 |
| LONG | 5 | 41 | 41 | 20 | 21 | 0.191009 | 0.228484 | 0.095202 | -0.132676 | -0.132676 |
| SHORT | 1 | 41 | 40 | 14 | 26 | 1.330199 | 0.222600 | 0.092750 | 1.014848 | 1.014848 |
| SHORT | 2 | 61 | 59 | 31 | 28 | 1.687012 | 0.326793 | 0.136164 | 1.224056 | 1.224056 |
| SHORT | 3 | 52 | 50 | 27 | 23 | 1.534484 | 0.278995 | 0.116248 | 1.139241 | 1.139241 |
| SHORT | 4 | 37 | 36 | 21 | 15 | 1.472454 | 0.196746 | 0.081978 | 1.193730 | 1.193730 |
| SHORT | 5 | 38 | 38 | 20 | 18 | 1.533104 | 0.208391 | 0.086830 | 1.237884 | 1.237884 |

#### validation: 100 USDT, środkowy koszt

| Kierunek | Ranking walidacji | Wystąpienia | Transakcje | Zyskowne | Stratne | Brutto USDT | Prowizje USDT | Wykonanie USDT | Netto USDT | Netto % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| LONG | 1 | 5 | 5 | 2 | 3 | 0.021685 | 0.029940 | 0.012475 | -0.020730 | -0.020730 |
| LONG | 2 | 5 | 5 | 2 | 3 | 0.021685 | 0.029940 | 0.012475 | -0.020730 | -0.020730 |
| LONG | 3 | 5 | 5 | 2 | 3 | 0.021685 | 0.029940 | 0.012475 | -0.020730 | -0.020730 |
| LONG | 4 | 5 | 5 | 2 | 3 | 0.021685 | 0.029940 | 0.012475 | -0.020730 | -0.020730 |
| LONG | 5 | 5 | 5 | 2 | 3 | 0.013400 | 0.027054 | 0.011273 | -0.024927 | -0.024927 |
| SHORT | 1 | 5 | 5 | 1 | 4 | -0.046509 | 0.027970 | 0.011654 | -0.086133 | -0.086133 |
| SHORT | 2 | 1 | 1 | 0 | 1 | -0.026400 | 0.005602 | 0.002334 | -0.034336 | -0.034336 |
| SHORT | 3 | 1 | 1 | 0 | 1 | -0.026400 | 0.005602 | 0.002334 | -0.034336 | -0.034336 |
| SHORT | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |
| SHORT | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |

#### test: 100 USDT, środkowy koszt

| Kierunek | Ranking walidacji | Wystąpienia | Transakcje | Zyskowne | Stratne | Brutto USDT | Prowizje USDT | Wykonanie USDT | Netto USDT | Netto % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| LONG | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |
| LONG | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |
| LONG | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |
| LONG | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |
| LONG | 5 | 4 | 4 | 2 | 2 | 0.001440 | 0.021371 | 0.008905 | -0.028836 | -0.028836 |
| SHORT | 1 | 5 | 5 | 2 | 3 | 0.068680 | 0.027771 | 0.011571 | 0.029337 | 0.029337 |
| SHORT | 2 | 1 | 1 | 1 | 0 | 0.063600 | 0.005612 | 0.002338 | 0.055650 | 0.055650 |
| SHORT | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |
| SHORT | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |
| SHORT | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 |

### Zamrożona para filtrów: test, wspólny portfel

| Kapitał | Transakcje | Zyskowne | Stratne | Brutto USDT | Prowizje USDT | Wykonanie USDT | Netto USDT | Netto % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20 | 5 | 2 | 3 | 0.068680 | 0.027771 | 0.011571 | 0.029337 | 0.146687 |
| 100 | 5 | 2 | 3 | 0.068680 | 0.027771 | 0.011571 | 0.029337 | 0.029337 |
| 500 | 5 | 2 | 3 | 0.068680 | 0.027771 | 0.011571 | 0.029337 | 0.005867 |
| 1000 | 5 | 2 | 3 | 0.068680 | 0.027771 | 0.011571 | 0.029337 | 0.002934 |

LONG: zero wejść na holdoucie. SHORT: 5, z czego 2 zyskowne i 3 stratne. Łączny drobny plus nie odwraca negatywnej walidacji i nie dowodzi wyeliminowania strat.

## Co odróżniało wygrane od przegranych
Tylko opis treningu, bez twierdzenia o przyczynowości; różnica standaryzowana średnich to opis, nie test istotności po wielokrotnym szukaniu.

| Kierunek | Cecha | Mediana zyskownych | Mediana stratnych | Różnica stand. |
| --- | --- | --- | --- | --- |
| LONG | width_pct | 0.737792 | 0.633985 | 0.503161 |
| LONG | body_pct | 0.321997 | 0.221665 | 0.474659 |
| LONG | atr_pct | 0.359279 | 0.296597 | 0.408431 |
| LONG | macd_delta_pct | 0.013063 | 0.007708 | 0.381142 |
| LONG | ma10_gap | 0.361389 | 0.254427 | 0.363295 |
| SHORT | stoch_kd | -7.689273 | -5.067905 | -0.359660 |
| LONG | ma20_gap | 0.509864 | 0.368888 | 0.295415 |
| SHORT | macd_delta_pct | -0.011932 | -0.003345 | -0.251118 |
| LONG | stoch_kd | 10.210213 | 8.568627 | 0.240119 |
| LONG | tp_sl_ratio | 1.145644 | 1.776316 | -0.210478 |

## Ruch ceny potrzebny do zarobku
Poniżej procent korzystnego ruchu ceny odniesienia przy założonych kosztach, netto liczone od **nominału pozycji**, bez funding. Zaokrąglenie do ticka może podnieść próg.

| Kierunek | Koszt/stronę % | Pokrycie kosztów % | +0,10% netto | +0,20% netto | +0,30% netto |
| --- | --- | --- | --- | --- | --- |
| LONG | 0 | 0.120072 | 0.220132 | 0.320192 | 0.420252 |
| LONG | 0.025000 | 0.170145 | 0.270255 | 0.370365 | 0.470475 |
| LONG | 0.050000 | 0.220242 | 0.320402 | 0.420562 | 0.520723 |
| SHORT | 0 | 0.119928 | 0.219868 | 0.319808 | 0.419748 |
| SHORT | 0.025000 | 0.169856 | 0.269746 | 0.369636 | 0.469526 |
| SHORT | 0.050000 | 0.219758 | 0.319598 | 0.419438 | 0.519279 |

To progi warunkowe, nie gwarancja rzeczywistego wykonania. Dla zysku od całego kapitału C przy jednej pozycji N: wymagany zwrot netto pozycji = cel_portfela × C/N. Np. przy 5 USDT pozycji, +0,10% kapitału 20/100/500/1000 wymaga +0,4%/+2%/+10%/+20% netto na tej pozycji.

## Znaczenie kwoty i decyzja
Większy kapitał nie zmienia opłaty procentowej ani wielkości zlecenia 5 USDT; ułatwia wolny budżet i oddala próg strat. Dla 100/500/1000 w holdoucie wejścia i wynik USDT są identyczne. Mniejszy minus procentowy wynika z większego mianownika, nie poprawy skuteczności. Nie ma tutaj rekomendowanej kwoty czyniącej strategię rentowną. **Nie dodawać teraz żadnego znalezionego filtra do LIVE.**

## Czego test nie odtworzył
- Historycznego składu TOP200 (lista wybrana na koniec okresu; survivorship/selection bias), historycznych zmian parametrów kontraktów i stawek indywidualnych/promocji.
- Bid/ask, spreadu, order book, opóźnień GitHub, 429, przerw skanera, odmów/amendowania i niepełnych wykonań. Przyjęto bezbłędne potwierdzanie ochrony oraz ticker = close; to uproszczenie.
- Funding oraz ewentualnych kosztów likwidacji; dźwignia 1× nie jest dowodem braku tych kosztów.
- Kolejności high/low; SL-first jest założeniem.
- Historycznego rankingu do rozstrzygania dokładnych remisów volume_strength: techniczny porządek symboli.
- Nie przeszukano ponad 4 warunków, innych niż wymienione wskaźników ani wszystkich progów.
- Nie testowano skalowania 5-USDT limitu pozycji wraz z kapitałem, bo zmieniłoby to obecną strategię.
- Nie można twierdzić, że te wyniki są dokładnym replayem wykonań LIVE ani że mały zysk filtrów utrzyma się poza tą próbką.

## Pliki i odtworzenie
`capital_comparison.csv` / `results.json`: wszystkie kwoty × kierunki × okresy × koszty. `patterns.csv`: top5 układów/kierunek dla wszystkich kapitałów; progi w `frozen_rules.json`. `indicator_separation.csv`, `real_trade_audit.json`, `provenance.json`. `candidate_parts/*.csv.gz`: wszystkie 19 005 scenariuszowych kandydatów z wskaźnikami, cenami i wynikami.

W checkoutcie wskazanego commitu, z tym katalogiem raportowym oraz już pobranymi archiwami rozpakowanymi do DATA_DIR:
```sh
python research/capital_replay_20261023/replay.py DATA_DIR research/capital_replay_20261023/instruments_snapshot.json
python research/capital_replay_20261023/analyze.py
```
Wymaga Python3.12 i numpy. Checkpointy są per coin. Nie uruchamia żadnych zleceń ani pobierania świec. Usuń checkpointy po zmianie kodu/danych/metadata. Raport odnotowuje 34/34 zaliczonych testów istniejących reguł tunelu.

## Kontrola archiwów
Potwierdzono SHA-256 wszystkich 11 archiwów. Brak duplikatów timestampów. Znaleziono 2 przerwy w siatce 5m: [{'inst': 'ZAMA-USDT', 'previous': 1787023500000, 'next': 1787024100000, 'candidate_trades_crossing': 0}, {'inst': 'SFP-USDT', 'previous': 1787664900000, 'next': 1787665500000, 'candidate_trades_crossing': 0}]
