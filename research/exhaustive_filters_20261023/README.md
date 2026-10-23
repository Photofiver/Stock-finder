# BloFin — audyt wyczerpującego przeszukiwania (NIEUKOŃCZONY)

[Wyniki i pokrycie](RESULTS.md) · [Pełny raport JSON](run/report.json) · [Manifest](run/manifest.json) · [Checkpoint](run/checkpoint.json)

LIVE i main nie zostały zmienione. Ten katalog zawiera wyłącznie program offline,
zamrożone dane, manifest przestrzeni, raport i przenośny checkpoint.
Nie importuje modułów LIVE, nie korzysta z kluczy i nie wysyła zleceń.

## Dokładny zakres

Źródło: Photofiver/Stock-finder, commit `fd6500af847bef6ad48fc7cdc3ad6f63da72215c`.
Przeczytano pełny `blofin_live_state.json`: 833 transakcje. Z tego 305 ma zapis
sygnału `PIVOT_TUNNEL_5M`. Pozostałe 528 to inne strategie: wyszczególniono je
w manifeście, a nie połączono ich wyników z tunelem. Dwa dzienniki zamknięć
22–23.10 sprawdzono jako źródło pomocnicze; nie dodano ich ponownie jako nowych transakcji.

Podział po czasie UK, ustalony przed optymalizacją:
- trening: 20–21.10.2026, 204 transakcje;
- walidacja: 22.10.2026, 57 transakcji;
- test: 23.10.2026 do zamrożonego stanu, 44 transakcje.

Wyniki pochodzą z rzeczywiście wykonanych historycznych transakcji różnych
wersji tunelu. NIE jest to ponowne uruchomienie aktualnej wersji bota na wszystkich
historycznych sygnałach. Odrzucenie pozycji mogłoby uwolnić kapitał dla sygnału,
którego wynik nie został zapisany. Tego efektu ten test nie rekonstruuje.

`source_inventory.json` zawiera pełny spis plików drzewa Git i ich SHA/rozmiary.
Jest to INWENTARYZACJA, nie dowód przeczytania wszystkich archiwów. Duże archiwa
skanów, wszystkie dawne wersje stanów Git i artefakty Actions nie zostały w całości
pobrane/przeliczone. Cały zakres pierwotnego żądania pozostaje niewykonany.

## Przestrzeń skończona

114 pól: wszystkie dopuszczone liczbowe i krótkie kategoryczne pola zapisanych
snapshotów wejścia (łącznie z wartościami list S/R i kotwic), plus jawne cechy
pochodne w `prepare()`. Pełna lista pól, wartości treningowych, liczności i
wyłączeń znajduje się w `run/manifest.json`. Brakujące dane pozostają brakujące.
Nie używamy nazwy monety, identyfikatorów, absolutnych czasów, cen po zamknięciu,
końcowych TP/SL ani maksimum osiągniętego podczas pozycji jako filtrów.

Dla pola liczbowego z n różnymi wartościami treningowymi tworzymy 2n+1
uporządkowanych komórek: otwarte przerwy i pojedyncze punkty równości.
Wszystkie ciągłe zakresy tych komórek reprezentują wszystkie progi jednostronne,
równości oraz zakresy z otwartymi/zamkniętymi końcami na tej siatce.
Liczba opcji R = 2 + (2n+1)(2n+2)/2: wyłączenie pola, brak wartości, każdy zakres.
Dla pola kategorycznego z q kategoriami R = 2^q + 1: wyłączenie pola,
brak wartości i każdy niepusty podzbiór kategorii. Nieznana kategoria nie spełnia
warunku na kategorie treningowe. Pola bez wartości treningowych mają tylko
wyłączenie/brak/obecność; nie tworzymy progów z danych testowych.

Każda kombinacja wybiera jedną opcję dla każdego pola. Aktywne warunki są
łączone AND. Wyłączamy tylko kombinację bez żadnego warunku (osobny baseline).
Nie ma limitu liczby aktywnych pól: sprawdzane są głębokości 1..114.
Powtórzenie kilku zakresów tego samego pola jest równoważne ich przecięciu;
zakresy puste nie dają transakcji. Arbitrary OR między różnymi polami, dowolne
nowe wzory wskaźników i nieskończenie wiele okresów NIE należą do tej przestrzeni.
Zatem licznik nie oznacza „wszystkich możliwych strategii matematycznych”.

Dokładna liczba kombinacji wynosi iloczyn wszystkich R minus jeden:

```text
54143781888837532874354084841791699861187843728090124385830220650112786959959865602643447453490354980589798534278942608888301967437588580663115307778172990184692453617677919791008019742397898761921673732429006563179694604788425721533573519929903242728008986786085977553740161609807611004382523682744638584917050656034230317648769208909774032469677771781673179278345419015445547224924159999999999999999999999
```

## Dowód i checkpoint

`weights[i][k]` liczy kombinacje z k aktywnymi polami w suffixie:
`weights[i][k] = weights[i+1][k] + (R[i]-1)*weights[i+1][k-1]`.
`unrank()` odwzorowuje każdy indeks od 0 do 54143781888837532874354084841791699861187843728090124385830220650112786959959865602643447453490354980589798534278942608888301967437588580663115307778172990184692453617677919791008019742397898761921673732429006563179694604788425721533573519929903242728008986786085977553740161609807611004382523682744638584917050656034230317648769208909774032469677771781673179278345419015445547224924159999999999999999999999-1 na dokładnie jedną kombinację,
w kolejności liczby aktywnych pól. To konstrukcja bijekcji, nie losowanie.
Bitmaski realizują dokładne przecięcia. Cache identycznych masek treningowych
nie zmienia wyniku i nie wykorzystuje wyników testowych.

Cztery procesy liczą rozłączne fragmenty. Przed zatwierdzeniem partii program
porównuje listę zwróconych indeksów z pełnym oczekiwanym przedziałem.
SQLite atomowo zapisuje przedział, liczbę wyników, SHA256 wyników, najlepsze
remisy i następny indeks. Raport sprawdza brak luk/nakładania, zakres indeksów,
sumę liczności oraz równość z 54143781888837532874354084841791699861187843728090124385830220650112786959959865602643447453490354980589798534278942608888301967437588580663115307778172990184692453617677919791008019742397898761921673732429006563179694604788425721533573519929903242728008986786085977553740161609807611004382523682744638584917050656034230317648769208909774032469677771781673179278345419015445547224924159999999999999999999999. `complete=true` może powstać wyłącznie po
pełnym pokryciu zdefiniowanej przestrzeni. Teraz jest FALSE.

Wyniki gorsze od dotychczasowego optimum treningowego są usuwane po policzeniu;
ich liczbę i hash zachowuje ledger. Nie pomija to testowania żadnego indeksu.
Wszystkie remisy najlepszego wyniku treningowego trafiają do `best_ties.jsonl`
i mają osobne wyniki walidacji/testu. Do raportu głównego wybieramy pierwszy
najprostszy remis wyłącznie według treningu i indeksu, nigdy wyniku holdout.
Bieżące podglądy holdout nie są używane przez algorytm. Ponieważ te daty były
już analizowane wcześniej, nie nazywamy ich dziewiczym testem prospektywnym.

## Kapitał i koszty

Start każdego okresu: 20 USDT. Jedna pozycja jednocześnie, stawka
`min(20, aktualny kapitał)`, bez lewara. Kolejność jest zgodna z rzeczywistymi
czasami otwarć; pozycje nachodzące na zajęty kapitał są pomijane. Kwota nigdy
nie jest mnożona przez liczbę jednoczesnych pozycji. PnL i rzeczywiste opłaty
skalujemy proporcjonalnie do historycznego nominału. Sprawdzamy zgodność
netto = brutto + podpisana opłata. Wynik jest modelem, nie obietnicą identycznych
filli przy 20 USDT; nie odtwarza zaokrągleń kontraktów, funding ani nowego poślizgu.

Priorytet: dodatnia liczba wygranych bez strat, następnie minimalna liczba strat,
następnie maksymalna liczba wygranych. Zerowe wejścia nie wygrywają z 1 wygraną/0 strat.

## Wznowienie

Python 3.10+ na Linux/macOS, tylko biblioteka standardowa. Z katalogu tego pliku:

```bash
python -m unittest -q test_search.py
python search.py run --out run --workers 4 --seconds 3600
python search.py report --out run
```

Jeśli `progress.sqlite` nie istnieje, program automatycznie odtwarza stan
z dostarczonego `run/checkpoint.json`, po sprawdzeniu hashy. Ponowne uruchomienie
zaczyna od następnego niezatwierdzonego indeksu. Przerwanie procesu może wymagać
ponownego policzenia ostatniej niezatwierdzonej partii, ale nie usuwa wyników
już zatwierdzonych. Przenosząc pracę na inny komputer kopiuj cały katalog `run`
po normalnym zakończeniu, albo spójny backup SQLite. Nie uruchamiaj dwóch
koordynatorów na tym samym katalogu. Wewnątrz działa wiele workerów.

Zwiększenie `--seconds` zmienia wyłącznie czas jednej porcji obliczeń, nie
przestrzeń ani maksymalną liczbę wskaźników. Można powtarzać polecenie dowolnie.
Po wyczerpaniu przestrzeni program kończy się sam. Nie zainstalowano harmonogramu
ani zadania działającego w tle po tej sesji.

## Rzeczywiste ograniczenie

54143781888837532874354084841791699861187843728090124385830220650112786959959865602643447453490354980589798534278942608888301967437588580663115307778172990184692453617677919791008019742397898761921673732429006563179694604788425721533573519929903242728008986786085977553740161609807611004382523682744638584917050656034230317648769208909774032469677771781673179278345419015445547224924159999999999999999999999 jest liczbą 407-cyfrową (około 5,414e406). Nawet 10^9 kombinacji na sekundę
oznacza około 1,716e390 lat enumeracji. Mechanizm jest wykonywalny i wznawialny,
ale nie czyni pełnego ukończenia praktycznie osiągalnym. Nie deklarujemy 100%.
Dodatkowo do pełnego replayu obecnego bota potrzebna jest odrębna rekonstrukcja
wszystkich okazji wejścia, odpowiednich świec, rankingów, kwotowań i historycznych
parametrów. `derived.signal_line_reward_risk` to geometria tunelu przy sygnale,
NIE odtworzone początkowe TP/SL z giełdy. Tego brakującego etapu nie maskujemy.
