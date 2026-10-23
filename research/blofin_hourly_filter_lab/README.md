# BloFin — badanie filtrów co godzinę (READ-ONLY)

## Co jest analizowane
- `blofin_live_state.json → trade_history`: potwierdzone zamknięcia, kwoty, rzeczywiste prowizje, wynik netto.
- `signal_snapshot`: wskaźniki i warunki zapisane przez LIVE **przed wejściem**. Wskaźniki pochodzą z 5m świec BloFin już pobranych przez bota. Nie są pobierane płatne wskaźniki TAAPI/TradingView.
- Warunki są dopuszczane wyłącznie przy prawidłowych znacznikach czasu. Brakujące dane i niezgodne PnL są odrzucane z jawnym powodem.
- **Strategie są rozdzielone**: bank filtrów dotyczy snapshotów `PIVOT_TUNNEL_5M`. Inne stare rodziny nie są używane do optymalizacji tej strategii.

## Przebieg
1. Pierwsze uruchomienie wyznacza i zapisuje granicę czasu, 48 predykatów i progi dobrane tylko z historycznych transakcji. Nie zmieniaj pliku `frozen_rulebook.json` bez rozpoczęcia osobnego eksperymentu.
2. Każda następna godzina sprawdza te same predykaty i wszystkie ich kombinacje od 1 do 4 warunków, dla wszystkich dotychczas dostępnych zamknięć.
3. Nowe zamknięcia po dacie zamrożenia stanowią osobną **prospektywną** próbę testową. Nie zmieniają progów ani rankingu filtrów treningowych.
4. Wysyłany jest raport tekstowy na dotychczasowy kanał BloFin. Dane zapisują się w:
   - `research/blofin_hourly_filter_lab/frozen_rulebook.json`
   - `research/blofin_hourly_filter_lab/latest.json`
   - `research/blofin_hourly_filter_lab/latest.txt`

Workflow: `.github/workflows/blofin_hourly_filter_lab.yml` — cron co godzinę około :17 UTC, zależnie od opóźnień GitHub Actions. Możliwe uruchomienie ręczne.

## Ograniczenia
- To **nie** jest sprawdzenie wszystkich `5.4×10^406` możliwych filtrów, tylko wyraźnie zdefiniowanego, skończonego banku. W raporcie są dokładne liczniki.
- Filtracja opiera się na rzeczywiście wykonanych transakcjach, nie przewiduje transakcji, które bot otworzyłby przy uwolnieniu kapitału.
- `portfolio20` jest symulacją **jednej** pozycji naraz z 20 USDT; historyczne stopy zwrotu skalowane są do stawki bez modelowania dodatkowego poślizgu, zaokrągleń i fundingu.
- Nie udowodniono gwarancji zysku ani zera strat.
- Historia bota ma ograniczenie 5000 pozycji; archiwum należy rozszerzyć, zanim zostaną wyparte starsze transakcje.
- Zapisane finalne TP i SL mogą być przesunięte po wejściu. Ten moduł nie przyjmuje ich za początkowe poziomy, dopóki nie ma dowodu.
- Moduł nie zawiera żadnych zapytań do prywatnego API giełdy, nie otwiera pozycji i nie modyfikuje istniejących procesów LIVE.

## Test lokalny
```sh
python -m unittest -v test_blofin_hourly_filter_lab.py
python blofin_hourly_filter_lab.py --out /tmp/filter_lab_smoke
```
