# Analiza ML BloFin: XGBoost + SHAP (READ ONLY)

Gotowy analizator porównuje zamknięte transakcje z istniejącego `blofin_live_state.json`. Wczytuje **faktyczne wejściowe parametry** zapisane przez bota, bierze późniejszy rzeczywisty wynik **netto po prowizji**, i analizuje zależności za pomocą XGBoost oraz TreeSHAP. Korzysta z istniejącej kontroli poprawności znaczników czasu `blofin_hourly_filter_lab.py`.

Dla coinów, dla których istnieje plik w 90-dniowym archiwum 5m GitHub Actions run **39994070142**, oblicza dodatkowo historyczne wartości ADX(14), RSI(14), MACD, wolumenu w stosunku do MA20, siły świecy, MA10/MA20 i trendu. Liczone są wyłącznie świece **zamknięte przed wejściem**. Żadnych nowych zapytań do BloFin.

## Uruchomienie i wynik

Po włączeniu na głównej gałęzi repo: **Actions → BloFin XGBoost SHAP win-loss (READ ONLY) → Run workflow**.

Z zakończonego uruchomienia otwórz **Artifacts → blofin-ml-winloss-report**. Znajdziesz:
- `report.md`: wyniki i najważniejsze wskaźniki;
- `summary.json`: liczby treningu, walidacji i testu;
- `median_winners_vs_losers.csv`: **mediany** dla wygranych oraz przegranych;
- `shap_importance.csv`: istotność wskaźników według TreeSHAP;
- `holdout_predictions.csv`: indywidualne decyzje na późniejszych, niewidzianych transakcjach.

Dane są podzielone chronologicznie: 60% trening, 20% walidacja progu, 20% ostateczny test. Uwzględniono 30-minutowe odstępy między okresami. Jeśli walidacja nie wykaże dodatniego wyniku dla progu z minimum pięcioma transakcjami, model nie rekomenduje wejść w teście. Raportuje rzeczywiste historyczne kwoty oraz **osobno** tylko hipotetyczny scenariusz 20 USDT z jedną pozycją naraz.

## Bezpieczeństwo i koszty

Workflow ma `contents: read` i `actions: read`, bez kluczy giełdy i bez zmiany LIVE, TP/SL, GitHub danych lub zleceń. Standardowy runner `ubuntu-24.04` w publicznym repo GitHub jest bezpłatny; biblioteki XGBoost i SHAP są otwartoźródłowe. Raport ma retencję 7 dni.

Uwaga: 90 dni świec to **nie** 90 dni faktycznie dokonanych transakcji. Próba obejmuje transakcje wykonane przez historyczne warianty bota. Wykaz 200 instrumentów został wybrany na koniec okresu, więc nie odtwarza dawnych list TOP200. Cena może nie uwzględniać intrabar poślizgów, a analiza nie zna wszystkich odrzuconych przez bota okazji. **Wskaźnik SHAP ani dobry wynik testu nie stanowią gwarancji przyszłego zysku.**
