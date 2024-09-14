import yfinance as yf
import pandas as pd

# Lista symboli akcji do analizy (S&P 500)
def get_sp500_symbols():
    table = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')
    df = table[0]
    return df['Symbol'].tolist()

# Sprawdzenie kryteriów
def check_criteria(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        # Metryki Wzrostu
        # Roczny wzrost sprzedaży: Co najmniej 10%
        revenue_growth = info.get('revenueGrowth')
        if revenue_growth is not None:
            growth_metric = revenue_growth >= 0.10
        else:
            growth_metric = False

        # Trzyletni wzrost zysków: Co najmniej 20% rocznie
        earnings_growth = info.get('earningsQuarterlyGrowth')
        if earnings_growth is not None:
            earnings_metric = earnings_growth >= 0.20
        else:
            earnings_metric = False

        # Zwrot z kapitału własnego (ROE): Co najmniej 15%
        roe = info.get('returnOnEquity')
        if roe is not None:
            roe_metric = roe >= 0.15
        else:
            roe_metric = False

        # Wskaźnik C/Z (Cena/Zysk): Mniejszy niż 20
        pe_ratio = info.get('trailingPE')
        if pe_ratio is not None:
            pe_metric = pe_ratio < 20
        else:
            pe_metric = False

        # Wskaźnik C/WK (Cena/Wartość Księgowa): Mniejszy niż 3
        pb_ratio = info.get('priceToBook')
        if pb_ratio is not None:
            pb_metric = pb_ratio < 3
        else:
            pb_metric = False

        # Stosunek Długu do Kapitału Własnego (Debt-to-Equity): Mniejszy niż 1
        debt_to_equity = info.get('debtToEquity')
        if debt_to_equity is not None:
            debt_equity_metric = debt_to_equity < 100  # Procent
        else:
            debt_equity_metric = False

        # Sprawdzanie wszystkich warunków
        if (growth_metric and earnings_metric and roe_metric and pe_metric and pb_metric and debt_equity_metric):
            return True
        else:
            return False

    except Exception as e:
        print(f"Błąd podczas analizy {ticker}: {e}")
        return False

def find_matching_stocks():
    matching_stocks = []
    symbols = get_sp500_symbols()

    for symbol in symbols:
        print(f"Analiza akcji: {symbol}")
        if check_criteria(symbol):
            matching_stocks.append(symbol)
            print(f"Akcja {symbol} spełnia wszystkie kryteria.\n")
        else:
            print(f"Akcja {symbol} nie spełnia wszystkich kryteriów.\n")

    # Zapisanie wyników do pliku
    if matching_stocks:
        with open('matching_stocks.txt', 'w') as f:
            for stock in matching_stocks:
                f.write(f"{stock}\n")
        print("Akcje spełniające wszystkie kryteria zostały zapisane w pliku 'matching_stocks.txt'.")
    else:
        print("Żadna akcja nie spełnia wszystkich kryteriów.")

if __name__ == "__main__":
    find_matching_stocks()
