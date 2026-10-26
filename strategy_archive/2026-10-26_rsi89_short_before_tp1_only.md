# Kopia zasad LIVE sprzed zmiany TP-only — 26.10.2026

Repo: `Photofiver/Stock-finder`.

Oryginalny plik: `blofin_volume_flip_both_live.py`
Oryginalny SHA bloba (GitHub): `28d9b0aa518d1ec3eab90b9a13c9d3010db81665`

**Stan zachowany w historii Git:** przed zmianą z 26.10.2026 nowe SHORT na zamkniętym 5m RSI(14) przecinającym 89 w górę otrzymywały broker TP 0,55% od ceny i SL 0,50% od ceny, a oprócz tego istniała automatyczna reguła wyjścia po przeciwnym wolumenie. LONG pozostały pivot/tunnel zgodnie ze swoim trybem.

**Nowa dyspozycja użytkownika:** wyłącznie dla NOWYCH SHORT RSI89: TP 1% od ceny wejścia (gross, przed prowizjami), bez SL oraz bez wolumenowego/pivotowego wyjścia; techniczne zamknięcie awaryjne pozostaje możliwe, jeśli nie da się potwierdzić przyjęcia TP przez giełdę. Istniejące pozycje i pozostałe strategie zachowują wcześniejsze ustawienia.

Commit zmiany: `b6e66242a22249f74de492aa33adc750693a3719`.

W celu przywrócenia poprzedniego kodu porównać odpowiednią wersję pliku z historią GitHub i zachować aktualne dane pozycji oraz historię transakcji bez nadpisywania.
