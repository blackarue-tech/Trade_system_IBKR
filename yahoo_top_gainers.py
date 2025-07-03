# yahoo_top_gainers.py - Fetches top US gainers using Yahoo Finance public API
import yfinance as yf
import pandas as pd

def get_yahoo_top_gainers_above_50(count=10):
    try:
        # Use yfinance's built-in tickers to avoid scraping rate limits
        tickers = yf.tickers_sp500()[:count]
        results = []
        for symbol in tickers:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period='1d')
            if not hist.empty:
                price = hist['Close'][-1]
                if price > 50:
                    results.append(f"{symbol} - ${price:.2f}")
        return results
    except Exception as e:
        print(f"❌ Failed to fetch Yahoo top gainers: {e}")
        return []

if __name__ == "__main__":
    top_gainers = get_yahoo_top_gainers_above_50(count=10)
    print("✅ Top Gainers Above $50:")
    for symbol in top_gainers:
        print(symbol)
        
