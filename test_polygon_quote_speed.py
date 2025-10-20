# test_polygon_quote_speed.py

import time
import json
from polygon_utils import get_polygon_quote

TOP_GAINERS_PATH = "exports/top_gainers.json"

# Load symbols from JSON file
with open(TOP_GAINERS_PATH) as f:
    data = json.load(f)

symbols_to_test = [item["symbol"] if isinstance(item, dict) else item for item in data.get("symbols", [])]

for symbol in symbols_to_test:
    start = time.time()
    quote = get_polygon_quote(symbol)
    end = time.time()

    elapsed_ms = (end - start) * 1000
    print(f"⏱️ {symbol} quote fetched in {elapsed_ms:.2f} ms")

    if quote:
        print(f"✅ {symbol} quote: bid={quote.get('bid')}, ask={quote.get('ask')}, last={quote.get('last')}\n")
    else:
        print(f"❌ {symbol} quote not retrieved\n")

