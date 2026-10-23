# BloFin LIVE — audit 90 dni, 4 kapitały (23.10.2026)

**Status: WYKONANO PRZYBLIŻONY REPLAY OHLCV, NIE WYKONANO PEŁNEJ SYMULACJI REALNYCH ZLECEŃ.**
LIVE nie został zmieniony. Badawcza gałąź `research/blofin-90d-capital-review-20261023` nie ma zmian w kodzie handlującym.

## Źródła

- Gotowe, niepobierane ponownie z giełdy archiwa: https://github.com/Photofiver/Stock-finder/actions/runs/39994070142 (10 shard ZIP + universe ZIP).
- 200 instrumentów, 5 142 770 świec 5m, 25 lipca–23 października 2026.
- Raport: `research/blofin_free_history/latest_summary.json`; 196 pełnych historii, 4 krótsze.
- Kod ACTIVE: `.github/workflows/blofin_live_signal_watch.yml`, `blofin_volume_flip_both_live.py`, `blofin_pivot_tunnel.py`, `blofin_live_hourly.py`. Zrewidowana gałąź `main` przy rozpoczęciu dokumentowania: `313a943e75f949056f0c8d02ce0b80eca04da0b3`. Kod LIVE `blofin_volume_flip_both_live.py` blob `5e13466d8ea0b9711f88944fd4dd7a508df2f43a`.
- `blofin_live_state.json` na main jest PUSTY (0 znaków). Do kontroli empirycznej prowizji użyto 57 zapisów z `blofin_fast_scan_events/trade_reviews/2026-10-23.jsonl`: 19 WIN, 38 LOSS, netto około −0,487371 USDT, prowizje około 0,324809 USDT, mediana opłaty obustronnej względem nominału ok. 0,11995%.

## Zakres repliki

Odtworzono potwierdzanie czterech pivotów na 48 zamkniętych świecach; oba kierunki; zmiana koloru świecy z większym wolumenem; priorytet wolumenu / wcześniejsza MA20; filtry LONG (opadająca MA20 wolumenu, TP do 0,60%, SL od 0,10%); wczesne TP 0,05% przed linią, odległość TP do wejścia powyżej 0,12%; ochronne SL i jego zawężanie; przybliżone drugie dotknięcie; przypadki TP i SL na tej samej świecy policzono po SL. Liczba potencjalnych sygnałów: 16 404 = 2 908 LONG + 13 496 SHORT. Podział: 10 933 / 2 815 / 2 656 sygnałów (60d / 15d / 15d).

**Wyniki portfela są scenariuszem, a nie faktyczną egzekucją**: cena wejścia = otwarcie kolejnej 5m (zamiast realnego BID/ASK kilka sekund po zamknięciu). Na SHORT wymagane rzeczywiste adverse gap 0,05–0,10% ma tylko PROXY z ceną kolejnego open; historyczny spread i poślizg nieznane. W tej wersji ok. 530/13 496 potencjalnych SHORT przechodzi przybliżony warunek ceny oferty. Nie posiadamy historycznych `contractValue/minSize/lotSize/tickSize` wszystkich 200 instrumentów: użyto frakcyjnego nominału jako założenia, więc nie wolno utożsamiać liczby transakcji z liczbą realnie wykonalnych zleceń.

W modelu podstawowym: dźwignia 1×, maks. cztery pozycje po 5 USDT, wspólny limit ekspozycji 20 USDT, bufor 0,50 USDT, **jednorazowy kumulacyjny risk stop −10% bankrolla LIVE, bez automatycznego resetu**. Dla większych kapitałów kod wciąż ogranicza strategię do 20 USDT. Model opłat: 0,06% taker przy wejściu i 0,06% przy wyjściu; osobno koszt dodatkowy 0,02% nominału roundtrip (tylko scenariusz, nie odtworzony spread).

## Porównanie — ten sam limit kodu (cały okres do aktywacji risk stop)

| Całkowity kapitał | Transakcje (proxy SHORT) | WIN / LOSS | Brutto USDT | Prowizje USDT | Dodatkowy koszt USDT | Netto USDT | ROI od całkowitego kapitału |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 20 | 273 | 95 / 178 | −0,10340 | 1,638 | 0,273 | **−2,01440** | −10,0720% |
| 100 | 268 | 91 / 177 | −0,12990 | 1,608 | 0,268 | **−2,00590** | −2,0059% |
| 500 | 268 | 91 / 177 | −0,12990 | 1,608 | 0,268 | **−2,00590** | −0,4012% |
| 1000 | 268 | 91 / 177 | −0,12990 | 1,608 | 0,268 | **−2,00590** | −0,2006% |

Różnica w ROI to głównie inny **mianownik całkowitego kapitału**, a nie poprawa skuteczności. 20 USDT może mieć mniej aktywnych pozycji przez bufor gotówki 0,50 USDT. Wartości modeli nie uwzględniają rzeczywistych zaokrągleń lota i poślizgu.

## Ostatnie 15 dni — niezależny od optymalizacji odcinek sygnałów

| Kapitał | Transakcje | WIN / LOSS | Brutto | Prowizje | Dodatkowe koszty | Netto | ROI |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 20 | 297 | 109 / 188 | +0,06410 | 1,782 | 0,297 | **−2,01490** | −10,0745% |
| 100 | 295 | 108 / 187 | +0,04608 | 1,770 | 0,295 | **−2,01892** | −2,0189% |
| 500 | 295 | 108 / 187 | +0,04608 | 1,770 | 0,295 | **−2,01892** | −0,4038% |
| 1000 | 295 | 108 / 187 | +0,04608 | 1,770 | 0,295 | **−2,01892** | −0,2019% |

Uwaga: to osobne uruchomienie portfela od początku 15-dniowego odcinka, z resetem kapitału testowego dla potrzeb porównania. Przy nieprzerwanym 90-dniowym uruchomieniu hard stop z początku badanej historii uniemożliwiłby kolejne wejścia.

## Filtry: 60 dni optymalizacji, 15 dni walidacji, 15 dni testu

Przesiano 1836 układów jedno- do czterowarunkowych przy prógach >=35 sygnałów treningowych i >=8 walidacyjnych. **Żaden z przesiewanych układów nie osiągnął dodatniej sumy netto na treningu** w prowizji 0,12% + dodatkowym 0,02% roundtrip (wstępna ocena pojedynczych sygnałów po 5 USDT, przed konkurencją o sloty). Najlepsze pod względem najmniejszej straty nie przeszły niezależnego testu z wynikiem netto > 0.

Przykładowe wybrane W CZASIE WALIDACJI konfiguracje w niezależnym 15-dniowym teście portfela 20 USDT, proxy SHORT, 0,14% łącznego kosztu za obrót tam i z powrotem:
- Wolumen >= wcześniejszej MA20, MACD hist > 0, ATR14 >= 0,3%, TP/SL 0,875–1,077: **11 transakcji, 4 WIN, 7 LOSS, netto −0,08878 USDT**.
- Wolumen >= 1,5 × wcześniejsza MA20, 3× wolumen poprzedniej świecy, MACD hist > 0, TP/SL 0,875–1,077: **13 transakcji, 4 WIN, 9 LOSS, netto −0,10019 USDT**.
- Wariant z 3× wolumenem, TP/SL >=1 oraz TP/SL 0,875–1,077: 6 transakcji, 1 WIN, 5 LOSS, netto −0,06188 USDT; próbka jest zbyt mała.

W samych sygnałach końcowego odcinka (nie wynik portfela), udział zyskownych po kosztach 0,14%: ogólnie 35,6%; wolumen >=1,5× MA20: 42,9% (105 sygnałów); wolumen sygnału >=2× poprzednia świeca: 40,5% (222 sygnały). Każda z tych grup nadal miała **ujemną średnią stopę zwrotu netto**. To skojarzenia, nie zweryfikowane filtry pozwalające zwiększyć zysk.

## Koszty i minimalny ruch

Przy taker 0,06% w obie strony i bez innego kosztu próg break even wynosi około **0,12%** ruchu instrumentu. Scenariusz +0,02% kosztu roundtrip zwiększa próg do około 0,14%, a +0,05% do około 0,17%. Żeby mieć +0,10%, +0,20%, +0,30% NETTO od nominału, wymagany ruch brutto przy dodatkowym koszcie 0,02% to około **0,24%, 0,34%, 0,44%** (bez funding; zależne od cen realizacji).

## Najważniejsze ograniczenia wiarygodności

1. Historyczne TOP200 z każdego dnia nie jest znane. Dzisiejsze TOP200 cofnięto na 90 dni, co powoduje **bias informacji o przyszłym doborze instrumentów**. Wynik ostatnich 15 dni jest niezależny dla wyboru filtrów, **ale nie niezależny dla składu universe**.
2. Nie odtworzono rzeczywistego BID/ASK, spreadu, tick/lot/minSize/contractValue, księgi zleceń, filli, fundingu, ewentualnych odrzuceń i opóźnień Actions.
3. Świeca 5m o obu dotknięciach OCO ma przypisane SL, więc wyniki są konserwatywne względem kolejności intra-candle; ruch open jako proxy może nadal istotnie zafałszować wynik.
4. 57 recenzji rzeczywistych pozycji z jednego dnia służyło wyłącznie do kontroli orientacyjnego poziomu prowizji; nie wystarcza do kalibracji pozostałych 90 dni i nie zawiera wszystkich wejściowych indykatorów. `blofin_live_state.json` jest puste.
5. Dynamiczne trailing OCO i drugi dotyk są odwzorowane jako symulacja świecowa, a nie identyczna historia zmian zleceń giełdy.

**Rekomendacja badawcza: nie dodawać filtrów do LIVE na podstawie tych wyników.** W niezależnym teście brak zyskownej, powtarzalnej kombinacji po kosztach. Do wiernego testu potrzebne są co najmniej historyczne rankingi TOP200, instrument metadata i kwotowania BID/ASK z momentów sygnałów; bez tego nie można spełnić wymogu rzeczywistych ograniczeń zleceń.

### Status realizacji
- **Wykonano:** odczyt wszystkich 11 archiwów, rekonstrukcja sygnałów, rozdział 60/15/15, LONG/SHORT, warianty kapitału, analiza kosztów, wstępna analiza kombinacji wskaźników, kontrola 57 realnych zamknięć, badawczy raport w GitHub.
- **Nie wykonano w wymaganej dokładności:** pełny broker-exact replay LIVE, wiarygodne historyczne spread/slippage/funding, historyczne minimalne ilości kontraktów wszystkich 200 coinów, potwierdzona zgodność każdej pozycji z rzeczywistą giełdą. Bez tych danych nie istnieje rzetelna tabela "realnie wykonalnych" pozycji dla wszystkich wariantów.
