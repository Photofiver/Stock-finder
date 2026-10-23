import json,hashlib,gzip,csv,io,math
from pathlib import Path
from datetime import datetime,timezone
O=Path(__file__).parent;R=json.load(open(O/'results.json'));F=json.load(open(O/'frozen_rules.json'));A=json.load(open(O/'real_trade_audit.json'))
def table(rows,cols):
 def fmt(v):return f'{v:.6f}' if isinstance(v,float) else str(v)
 return '| '+' | '.join(a for a,b in cols)+' |\n| '+' | '.join('---' for c in cols)+' |\n'+'\n'.join('| '+' | '.join(fmt(r.get(b,'')) for a,b in cols)+' |' for r in rows)+'\n'
def sel(**kw):return [x for x in R['results'] if all(x[k]==v for k,v in kw.items())]
p=['# BloFin LIVE — test kapitału 20/100/500/1000 USDT\n', '**Werdykt: nie wdrażać znalezionych filtrów. Brak potwierdzonej przewagi na walidacji. LIVE nie zmieniono.**\n',
'''To replay obecnych reguł na danych OHLCV, nie rekonstrukcja rzeczywistych wykonań. Wynik netto oznacza wynik po prowizji i ZAŁOŻONYM koszcie wykonania, **bez historycznego funding**, którego nie ma w archiwach. Nie jest to wynik po wszystkich rzeczywistych kosztach.

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
''']
cols=[('Kapitał','capital'),('Transakcje','n'),('Zyskowne','wins'),('Stratne','losses'),('Brutto USDT','gross_usdt'),('Prowizje USDT','fees_usdt'),('Wykonanie USDT','friction_usdt'),('Netto USDT','net_usdt'),('Netto %','net_pct')]
p.append(table(sel(mode='baseline',side='BOTH',period='all90',slip_per_side_pct=.025),cols))
p.append('## Obecna strategia: niezależne ostatnie 15 dni\n');p.append(table(sel(mode='baseline',side='BOTH',period='test',slip_per_side_pct=.025),cols))
p.append('## LONG i SHORT osobno — ostatnie 15 dni, kapitał 100 USDT\n');p.append(table([x for x in R['results'] if x['mode']=='baseline' and x['period']=='test' and x['capital']==100 and x['side']!='BOTH' and x['slip_per_side_pct']==.025],[('Kierunek','side')]+cols[1:]))
p.append('## Wrażliwość na koszty: całe 90 dni\n');p.append(table([x for x in R['results'] if x['mode']=='baseline' and x['side']=='BOTH' and x['period']=='all90'],[('Koszt/stronę %','slip_per_side_pct')]+cols))
p.append('## Układy i selekcja filtrów\n14 cech: RSI14, Stoch K, K−D, ATR14%, volume/MA20, volume/poprzedni volume, korpus%, szerokość tunelu%, odległości od SMA10 i SMA20, MACD histogram%, zmiana histogramu%, OBV5/MA20 volume i stosunek odległości TP/SL. Każda cecha: kwartyle z treningu, <= lub >=. Kombinacje do 4 różnych cech, beam search 40, minimum 30 kandydatów treningowych. To **ograniczone przeszukiwanie**, nie wszystkie możliwe wskaźniki/progi/kombinacje.\n')
p.append(table([dict(side=s,**v) for s,v in R['coverage']['search'].items()],[('Kierunek','side'),('Sprawdzone rozszerzenia','evaluated'),('Unikalne maski','unique_masks'),('Finaliści walidacji','validation_finalists')]))
p.append('Wybrano jedną regułę na kierunek według walidacyjnego NET przy 100 USDT i >=5 zamknięć; progi zamrożono przed raportowaniem testu. **Obie wybrane reguły tracą na walidacji, więc żadna nie kwalifikuje się do LIVE.**\n')
for side,v in F['selected'].items():p.append(f"### {side}: wybrany kandydat badawczy, nie rekomendacja\n"+' AND '.join(f'{k} {op} {value:.12g}' for k,op,value in v['rule'])+'\n')
p.append('### Pięć najwyżej sklasyfikowanych kandydatów na każdy kierunek\nKolejność z walidacji; bez sortowania po niezależnym teście. Wiersze ALL90 zawierają trening i nie są dowodem skuteczności. Różne progi mogą przepuścić te same transakcje.\n')
for period in ['all90','validation','test']:
 p.append(f'#### {period}: 100 USDT, środkowy koszt\n');p.append(table([x for x in R['patterns'] if x['capital']==100 and x['period']==period],[('Kierunek','side'),('Ranking walidacji','validation_rank'),('Wystąpienia','occurrences')]+cols[1:]))
p.append('### Zamrożona para filtrów: test, wspólny portfel\n');p.append(table(sel(mode='filtered',side='BOTH',period='test',slip_per_side_pct=.025),cols))
p.append('LONG: zero wejść na holdoucie. SHORT: 5, z czego 2 zyskowne i 3 stratne. Łączny drobny plus nie odwraca negatywnej walidacji i nie dowodzi wyeliminowania strat.\n')
p.append('## Co odróżniało wygrane od przegranych\nTylko opis treningu, bez twierdzenia o przyczynowości; różnica standaryzowana średnich to opis, nie test istotności po wielokrotnym szukaniu.\n');p.append(table(R['separation'][:10],[('Kierunek','side'),('Cecha','feature'),('Mediana zyskownych','winner_median'),('Mediana stratnych','loser_median'),('Różnica stand.','standardized_mean_difference')]))
p.append('## Ruch ceny potrzebny do zarobku\nPoniżej procent korzystnego ruchu ceny odniesienia przy założonych kosztach, netto liczone od **nominału pozycji**, bez funding. Zaokrąglenie do ticka może podnieść próg.\n')
breaks=[]
for side in ['LONG','SHORT']:
 for s in [0,.00025,.0005]:
  row={'side':side,'slip':100*s}
  for target in [0,.001,.002,.003]:
   r=((1+.0006+target)/(1-.0006)*(1+s)/(1-s)-1) if side=='LONG' else (1-(1-.0006-target)/(1+.0006)*(1-s)/(1+s))
   row[str(target)]=r*100
  breaks.append(row)
p.append(table(breaks,[('Kierunek','side'),('Koszt/stronę %','slip'),('Pokrycie kosztów %','0'),('+0,10% netto','0.001'),('+0,20% netto','0.002'),('+0,30% netto','0.003')]))
p.append('To progi warunkowe, nie gwarancja rzeczywistego wykonania. Dla zysku od całego kapitału C przy jednej pozycji N: wymagany zwrot netto pozycji = cel_portfela × C/N. Np. przy 5 USDT pozycji, +0,10% kapitału 20/100/500/1000 wymaga +0,4%/+2%/+10%/+20% netto na tej pozycji.\n')
p.append('## Znaczenie kwoty i decyzja\nWiększy kapitał nie zmienia opłaty procentowej ani wielkości zlecenia 5 USDT; ułatwia wolny budżet i oddala próg strat. Dla 100/500/1000 w holdoucie wejścia i wynik USDT są identyczne. Mniejszy minus procentowy wynika z większego mianownika, nie poprawy skuteczności. Nie ma tutaj rekomendowanej kwoty czyniącej strategię rentowną. **Nie dodawać teraz żadnego znalezionego filtra do LIVE.**\n')
p.append('## Czego test nie odtworzył\n- Historycznego składu TOP200 (lista wybrana na koniec okresu; survivorship/selection bias), historycznych zmian parametrów kontraktów i stawek indywidualnych/promocji.\n- Bid/ask, spreadu, order book, opóźnień GitHub, 429, przerw skanera, odmów/amendowania i niepełnych wykonań. Przyjęto bezbłędne potwierdzanie ochrony oraz ticker = close; to uproszczenie.\n- Funding oraz ewentualnych kosztów likwidacji; dźwignia 1× nie jest dowodem braku tych kosztów.\n- Kolejności high/low; SL-first jest założeniem.\n- Historycznego rankingu do rozstrzygania dokładnych remisów volume_strength: techniczny porządek symboli.\n- Nie przeszukano ponad 4 warunków, innych niż wymienione wskaźników ani wszystkich progów.\n- Nie testowano skalowania 5-USDT limitu pozycji wraz z kapitałem, bo zmieniłoby to obecną strategię.\n- Nie można twierdzić, że te wyniki są dokładnym replayem wykonań LIVE ani że mały zysk filtrów utrzyma się poza tą próbką.\n\n## Pliki i odtworzenie\n`capital_comparison.csv` / `results.json`: wszystkie kwoty × kierunki × okresy × koszty. `patterns.csv`: top5 układów/kierunek dla wszystkich kapitałów; progi w `frozen_rules.json`. `indicator_separation.csv`, `real_trade_audit.json`, `provenance.json`. `candidate_parts/*.csv.gz`: wszystkie 19 005 scenariuszowych kandydatów z wskaźnikami, cenami i wynikami.\n\nW checkoutcie wskazanego commitu, z tym katalogiem raportowym oraz już pobranymi archiwami rozpakowanymi do DATA_DIR:\n```sh\npython research/capital_replay_20261023/replay.py DATA_DIR research/capital_replay_20261023/instruments_snapshot.json\npython research/capital_replay_20261023/analyze.py\n```\nWymaga Python3.12 i numpy. Checkpointy są per coin. Nie uruchamia żadnych zleceń ani pobierania świec. Usuń checkpointy po zmianie kodu/danych/metadata. Raport odnotowuje 34/34 zaliczonych testów istniejących reguł tunelu.\n')
(O/'README.md').write_text('\n'.join(p))
# Smaller independent gzip parts remain directly recoverable through GitHub blobs.
parts=O/'candidate_parts';parts.mkdir(exist_ok=True)
with open(O/'candidate_trades.csv') as f:
 reader=csv.DictReader(f);rows=list(reader);cols=reader.fieldnames
for i in range(10):
 buf=io.StringIO();w=csv.DictWriter(buf,fieldnames=cols);w.writeheader();w.writerows(rows[i::10])
 with gzip.open(parts/f'candidates_{i:02}.csv.gz','wb') as f:f.write(buf.getvalue().encode())
json.dump(breaks,open(O/'cost_thresholds.json','w'),indent=2)
