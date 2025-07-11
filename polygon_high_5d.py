# polygon_high_5d.py - Fetches and caches 5-day highs for your pipeline

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
CACHE_PATH = "exports/high_5d_cache.json"

@fail_safe(module="Polygon5D", critical=True)
def fetch_5d_high(symbol):
    """
    Fetches 5-day daily bars and returns the highest close or high as 5D high.
    """
    end_date = datetime.now(LOCAL_TZ).date()
    start_date = end_date - timedelta(days=7)  # 7 calendar days to ensure 5 market days
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}"
        f"?adjusted=true&sort=desc&limit=5000&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "results" not in data or not data["results"]:
            log(f"No data found for {symbol} for 5D high.", "WARNING", "Polygon5D")
            return None
        high_5d = max(item["h"] for item in data["results"] if "h" in item)
        log(f"✅ 5D high for {symbol}: {high_5d}", "INFO", "Polygon5D")
        return high_5d
    except Exception as e:
        log(f"Error fetching 5D high for {symbol}: {e}", "ERROR", "Polygon5D")
        return None

@fail_safe(module="Polygon5D", critical=True)
def get_cached_5d_high(symbol):
    """
    Retrieves the cached 5D high if available, otherwise fetches and caches it.
    """
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)
    else:
        cache = {}

    if symbol in cache:
        return cache[symbol]

    high_5d = fetch_5d_high(symbol)
    if high_5d:
        cache[symbol] = high_5d
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    return high_5d

if __name__ == "__main__":
    test_symbols = ["EQ", "MSFT", "TSLA"]
    for symbol in test_symbols:
        high = get_cached_5d_high(symbol)
        print(f"{symbol} 5D High: {high}")
