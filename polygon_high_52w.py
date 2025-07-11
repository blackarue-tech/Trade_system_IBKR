# polygon_high_52w.py - Fetches and caches 52W highs for your pipeline

import os
import json
from datetime import datetime, timedelta
import requests
from pytz import timezone
from modules.log_utils import log, fail_safe
from dotenv import load_dotenv
load_dotenv()

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
LOCAL_TZ = timezone("America/Toronto")
CACHE_PATH = "exports/high_52w_cache.json"

@fail_safe(module="Polygon52W", critical=True)
def fetch_52w_high(symbol):
    """
    Fetches 1-year daily bars and returns the highest close or high as 52W high.
    """
    end_date = datetime.now(LOCAL_TZ).date()
    start_date = end_date - timedelta(days=365)
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}"
        f"?adjusted=true&sort=desc&limit=50000&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "results" not in data or not data["results"]:
            log(f"No data found for {symbol} for 52W high.", "WARNING", "Polygon52W")
            return None
        high_52w = max(item["h"] for item in data["results"] if "h" in item)
        log(f"✅ 52W high for {symbol}: {high_52w}", "INFO", "Polygon52W")
        return high_52w
    except Exception as e:
        log(f"Error fetching 52W high for {symbol}: {e}", "ERROR", "Polygon52W")
        return None

@fail_safe(module="Polygon52W", critical=True)
def get_cached_52w_high(symbol):
    """
    Retrieves the cached 52W high if available, otherwise fetches and caches it.
    """
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)
    else:
        cache = {}

    if symbol in cache:
        return cache[symbol]

    high_52w = fetch_52w_high(symbol)
    if high_52w:
        cache[symbol] = high_52w
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    return high_52w

if __name__ == "__main__":
    test_symbols = ["AAPL", "MSFT", "TSLA"]
    for symbol in test_symbols:
        high = get_cached_52w_high(symbol)
        print(f"{symbol} 52W High: {high}")
