# Wynik: NIEUKOŃCZONY

Nie znaleziono potwierdzonego filtra eliminującego straty na niezależnych okresach.
Nie zmieniono LIVE ani gałęzi main. Kod, dane i checkpoint znajdują się wyłącznie
na gałęzi badawczej.

| Kontrola | Wynik |
|---|---:|
| Liczba pól | 114 |
| Kombinacje policzone | 5,456,000 |
| Pokrycie całej zdefiniowanej przestrzeni | 1.00768727445E-398% |
| Pełne ukończenie | NIE |
| Potwierdzenie braku pominięć całej przestrzeni | NIE — pozostały nieobliczone kombinacje |
| Brak luk i powtórzeń w ukończonym prefiksie | TAK |
| Następny indeks (numeracja od zera) | 5456000 |
| Wszystkie pojedyncze warunki | 5 034 323 policzone |
| Kombinacje dwóch warunków | 421 677 policzone |
| Głębokości 3–114 | jeszcze niepoliczone, obsługiwane przez program |
| Testy programu | 9/9 zaliczonych |

Dokładny rozmiar (iloczyn liczności opcji wszystkich 114 pól minus jeden):

```text
54143781888837532874354084841791699861187843728090124385830220650112786959959865602643447453490354980589798534278942608888301967437588580663115307778172990184692453617677919791008019742397898761921673732429006563179694604788425721533573519929903242728008986786085977553740161609807611004382523682744638584917050656034230317648769208909774032469677771781673179278345419015445547224924159999999999999999999999
```

Każda liczność, próg i dopuszczalna wartość jest w [manifeście](run/manifest.json).
Pełny raport z licznikami każdej głębokości: [report.json](run/report.json).
Przestrzeń oznacza koniunkcje filtrów na zdefiniowanej siatce dostępnych snapshotów;
nie oznacza wszystkich wyobrażalnych wskaźników i formuł ani pełnego replayu rynku.

## Najlepszy dotychczas — wybrany wyłącznie na treningu

`173 < previous_opposite_volume <= 213`.
To surowy wolumen wcześniejszej świecy przeciwnego koloru, w jednostkach zapisanych
przez bota. Wartości między różnymi instrumentami nie są znormalizowane.
Jest to jeden z czterech remisów treningowych; wszystkie sprawdzono osobno
w [best_ties.jsonl](run/best_ties.jsonl).

| Okres UK | Wygrane | Straty | Wynik netto USDT | Wynik netto % z 20 USDT |
|---|---:|---:|---:|---:|
| 20–21.10, trening | 6 | 0 | +0.29971804 | +1.49859% |
| 22.10, walidacja | 0 | 3 | -0.30967330 | -1.54837% |
| 23.10, test | 0 | 0 | +0.00000000 | +0.00000% |

Na 23.10 filtr nie dopuścił żadnej transakcji. To nie potwierdza skuteczności.
Zero strat na treningu nie utrzymało się w walidacji. Nie rekomendujemy integracji.

Model wykorzystuje wspólne 20 USDT, jedną pozycję naraz i rzeczywiste podpisane
opłaty proporcjonalnie do nominału. Nie używa 20 USDT dla każdej równoczesnej pozycji.
Pełna definicja modelu i ograniczenia są w [README](README.md).

## Źródła i luki

Przeczytano 833 transakcje; do tej rodziny strategii należy 305:
204 treningowe, 57 walidacyjnych i 44 testowe. 528 transakcji innych strategii
pozostawiono w inwentarzu wyłączeń. Odczytano również dzienniki 22 i 23 października.
Nie wczytano wszystkich 712 MB archiwów skanowania, wszystkich historycznych
stanów Git ani artefaktów Actions. Nie odtworzono oryginalnych TP/SL ani
kontrfaktycznych pozycji, których LIVE nie otworzył. Dawne wyniki tunelu powstały
przy zmieniających się zasadach wyjścia. Wymaganie „wszystkie dane i obecna wersja
bota” nie zostało w pełni spełnione.

## Wznowienie bez utraty zatwierdzonych wyników

Z tego katalogu:

```bash
python search.py run --out run --workers 4 --seconds 3600
```

Program wczyta przenośny checkpoint, jeżeli nie ma lokalnej bazy SQLite.
Każde kolejne wywołanie kontynuuje od następnego indeksu, bez limitu głębokości.
Raport i checkpoint zawierają sumy kontrolne, indeksy i liczniki partii.
Nie uruchomiono stałego zadania ani harmonogramu po zakończeniu tej sesji.
Praktyczne ograniczenie jest kombinatoryczne: około 5,414e406 wariantów,
czyli około 1,716e390 lat nawet przy miliardzie wariantów na sekundę.
