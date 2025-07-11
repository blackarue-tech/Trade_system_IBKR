from modules.trend_scanner import get_positive_trending_stocks

SYMBOL_LIST = ["AAPL", "MSFT", "GOOGL", "NVDA", "TSLA"]  # replace with your list or top US symbols

trending = get_positive_trending_stocks(SYMBOL_LIST, min_pct_gain=0.3)

print("📈 Positive trending stocks (last 30 min):")
for stock in trending:
    print(stock)