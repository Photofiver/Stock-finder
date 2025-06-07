import yfinance as yf
import pandas as pd

# Lista symboli akcji do analizy (S&P 500)
def get_sp500_symbols():
    """Return a list of tickers for companies in the S&P 500 index."""
    return pd.read_html(
        'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    )[0]['Symbol'].tolist()

# Sprawdzenie kryteriów
def check_criteria(ticker):
    """Check whether the given ticker meets all screening criteria."""
    try:
        info = yf.Ticker(ticker).info

        greater_than = {
            'revenueGrowth': 0.10,          # Roczny wzrost sprzedaży
            'earningsQuarterlyGrowth': 0.20,  # Trzyletni wzrost zysków
            'returnOnEquity': 0.15           # Zwrot z kapitału własnego
        }

        less_than = {
            'trailingPE': 20,   # Wskaźnik C/Z
            'priceToBook': 3,   # Wskaźnik C/WK
            'debtToEquity': 100 # Stosunek Długu do Kapitału Własnego
        }

        for key, threshold in greater_than.items():
            if info.get(key, 0) < threshold:
                return False

        for key, threshold in less_than.items():
            value = info.get(key)
            if value is None or value >= threshold:
                return False

        return True

    except Exception as e:
        print(f"Błąd podczas analizy {ticker}: {e}")
        return False

def find_matching_stocks():
    symbols = get_sp500_symbols()
    matching_stocks = []

    for symbol in symbols:
        print(f"Analiza akcji: {symbol}")
        if check_criteria(symbol):
            matching_stocks.append(symbol)
            print(f"Akcja {symbol} spełnia wszystkie kryteria.\n")
        else:
            print(f"Akcja {symbol} nie spełnia wszystkich kryteriów.\n")

    if matching_stocks:
        with open('matching_stocks.txt', 'w') as f:
            f.write('\n'.join(matching_stocks))
        print("Akcje spełniające wszystkie kryteria zostały zapisane w pliku 'matching_stocks.txt'.")
    else:
        print("Żadna akcja nie spełnia wszystkich kryteriów.")

if __name__ == "__main__":
    find_matching_stocks()
