# BloFin LIVE — dziennik analiz zamkniętych pozycji

Każdy wiersz pliku `RRRR-MM-DD.jsonl` odpowiada jednej zweryfikowanej, zamkniętej pozycji na BloFin. Dzień i godziny zapisano w czasie UK (Europe/London). Bot dopisuje dane podczas skanu po zamknięciu pozycji; skan działa zwykle po każdej zamkniętej świecy 5-minutowej, a GitHub Actions zapisuje plik w repozytorium.

Pola: kierunek LONG/SHORT, czas wejścia i wyjścia, ceny, ostatni zapisany SL/TP, wynik brutto i netto, prowizje, najlepsza cena i maksymalny zaobserwowany profit brutto (przybliżenie na bazie świec 1m), oddany ruch ceny, przyczyna zamknięcia z bota, komentarz o potencjalnym braku ochrony zysku, odnośniki do wykresów. Kolumna `had_gross_profit_before_net_loss` pozwala szybko odnaleźć przypadki takie jak INIT-USDT.

Uwagi: maksimum i minimum świecy 1m nie podaje dokładnego momentu wewnątrz świecy. Wirtualny TP może nie być wykonywalnym poziomem. `external close` nie oznacza automatycznie SL — trzeba mieć potwierdzenie typu wykonania na giełdzie. Raporty są WYŁĄCZNIE obserwacyjne i nie zmieniają zleceń, ryzyka ani strategii.

Historia transakcji i kompletne dane pozostają także w `blofin_live_state.json`, w tablicy `trade_history`.
