# Weryfikacja

`python -m unittest -q test_search.py`: 9 testów zaliczonych.

- Bijekcja wszystkich 149 kombinacji małej przestrzeni vs niezależny itertools.product.
- Wszystkie granice otwartych i zamkniętych zakresów, także wartości niewidziane i braki.
- Przecięcia bitmask vs niezależne przecięcia zbiorów.
- Wspólny kapitał, nachodzące pozycje i opłaty.
- Odrzucenie przyszłego potwierdzenia pivotu i pól po wejściu.
- Niekompletna partia workera nie może przesunąć checkpointu.
- Przerwanie, wznowienie i brak podwójnego liczenia.
- Odtworzenie z przenośnego checkpointu po usunięciu lokalnej bazy.
- Odrzucenie zmienionych danych o niezgodnym SHA256.

Dodatkowo sprawdzono unikalność 305 identyfikatorów, chronologię wszystkich
305 rekordów i zgodność brutto + podpisana opłata = netto.
100% w testach syntetycznych dotyczy wyłącznie 149 sztucznych kombinacji,
NIE rzeczywistej przestrzeni BloFin.
