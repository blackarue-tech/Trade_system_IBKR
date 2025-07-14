# polygon_utils.py - Structured logging + fail-safe upgrades

import requests
import pandas as pd
import os
import json
import time
from datetime import datetime, timedelta
from pytz import timezone
from modules.price_data import get_price_data
from modules.log_utils import log, fail_safe
from dotenv import load_dotenv


load_dotenv()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
LOCAL_TZ = timezone("America/Toronto")
JSON_PATH = "exports/top_gainers.json"
CACHE_PATH = "exports/high_5d_cache.json"

if not POLYGON_API_KEY:
    log("⚠️ POLYGON_API_KEY not set. Check your .env file.", "WARNING", "PolygonUtils")
else:
    log("✅ POLYGON_API_KEY loaded successfully.", "INFO", "PolygonUtils")

@fail_safe(module="PolygonUtils")
def get_top_gainers_polygon(limit=13, save_json_path="exports/top_gainers.json"):
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={POLYGON_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        gainers = data.get("tickers", [])

        if not gainers:
            log(f"No gainers found in Polygon response. Response: {json.dumps(data)}", "WARNING", "PolygonUtils")
            return pd.DataFrame()

        gainers_list = []
        for g in gainers[:limit * 2]:
            ticker = g.get("ticker")
            change_pct = g.get("todaysChangePercent", 0)

            last_price = 0

            # Use last quote as fallback if lastTrade missing
            if "lastTrade" in g and isinstance(g["lastTrade"], dict):
                last_price = g["lastTrade"].get("p") or g["lastTrade"].get("price") or 0

            # Fallback: use g["day"]["c"] if available (closing price)
            if not last_price:
                day_data = g.get("day", {})
                last_price = day_data.get("c", 0)

            # Final fallback: skip if still zero
            if not last_price or last_price == 0:
                log(f"⚠️ Skipping {ticker}, no last price available.", "WARNING", "PolygonUtils")
                continue
            day_data = g.get("day", {})
            volume = day_data.get("v", 0)

            if ticker and last_price and volume:
                gainers_list.append({
                    "symbol": ticker,
                    "change_pct": change_pct,
                    "last_price": last_price,
                    "volume": volume
                })
            else:
                log(f"Skipped entry due to missing data: ticker={ticker}, last_price={last_price}, volume={volume}", "WARNING", "PolygonUtils")

        if not gainers_list:
            log("No valid gainers extracted after parsing.", "WARNING", "PolygonUtils")
            return pd.DataFrame()

        df = pd.DataFrame(gainers_list)

        if "last_price" in df.columns and not df.empty:
            df = df.sort_values(by="last_price", ascending=False).head(limit)
        else:
            log("'last_price' column missing or no data, skipping sort.", "WARNING", "PolygonUtils")

        log(f"Retrieved {len(df)} top gainers from Polygon.", "INFO", "PolygonUtils")

        symbols_payload = {
            "date": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"),
            "symbols": [{"symbol": s} for s in df["symbol"].tolist()]
        }
        os.makedirs(os.path.dirname(save_json_path), exist_ok=True)
        with open(save_json_path, "w") as f:
            json.dump(symbols_payload, f, indent=2)
        log(f"Saved top gainers to {save_json_path}", "INFO", "PolygonUtils")

        return df

    except Exception as e:
        log(f"Error retrieving top gainers: {e}", "ERROR", "PolygonUtils")
        return pd.DataFrame()

@fail_safe(module="PolygonUtils")
def ensure_gainers_symbols(json_path="exports/top_gainers.json", retries=3, wait_secs=5, limit=10):
    """
    Repeatedly attempts to load gainers symbols from the JSON.
    If empty or error, re-runs get_top_gainers_polygon until valid data is fetched.
    """
   

    for attempt in range(1, retries + 1):
        momentum_symbols, non_momentum_symbols = load_gainers_symbols(json_path)
        if momentum_symbols or non_momentum_symbols:
            log(f"✅ Successfully loaded gainers on attempt {attempt}.", "INFO", "PolygonUtils")
            return momentum_symbols, non_momentum_symbols

        log(f"⚠️ Attempt {attempt}: No valid gainers loaded. Refreshing from Polygon...", "WARNING", "PolygonUtils")
        df = get_top_gainers_polygon(limit=limit, save_json_path=json_path)

        if not df.empty:
            log(f"✅ Retrieved fresh gainers from Polygon on attempt {attempt}.", "INFO", "PolygonUtils")
            # Attempt to reload immediately after fresh fetch
            momentum_symbols, non_momentum_symbols = load_gainers_symbols(json_path)
            if momentum_symbols or non_momentum_symbols:
                return momentum_symbols, non_momentum_symbols

        log(f"⏳ Waiting {wait_secs}s before retrying...", "INFO", "PolygonUtils")
        time.sleep(wait_secs)

    log(f"❌ Failed to load valid gainers after {retries} attempts. Returning empty lists.", "ERROR", "PolygonUtils")
    return [], []

@fail_safe(module="PolygonUtils")
def filter_gainers_for_momentum(json_path="exports/top_gainers.json",
                                slope_threshold=0.005,
                                rolling_return_threshold=0.01,
                                volatility_max=0.04,
                                rvol_threshold=1.5,
                                min_score=4):
    if not os.path.exists(json_path):
        log(f"Momentum Scanner: {json_path} not found.", "ERROR", "PolygonUtils")
        return

    with open(json_path, "r") as f:
        data = json.load(f)

    updated_symbols = []

    for item in data.get("symbols", []):
        symbol_obj = {"symbol": item} if isinstance(item, str) else item
        symbol = symbol_obj["symbol"]

        df = get_price_data(symbol, days=1, interval="minute")
        if df is None or df.empty or len(df) < 20:
            symbol_obj["in_momentum"] = False
            updated_symbols.append(symbol_obj)
            continue

        closes = df["close"]
        slope = (closes.iloc[-1] - closes.iloc[-15]) / closes.iloc[-15]
        rolling_return = closes.pct_change(15).iloc[-1]
        volatility = closes.tail(15).std() / closes.tail(15).mean()

        avg_vol = df["volume"].rolling(20).mean().iloc[-1]
        current_vol = df["volume"].iloc[-1]
        rvol = current_vol / avg_vol if avg_vol else 0

        vwap = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        price = closes.iloc[-1]

        last_open = df["open"].iloc[-1]
        last_close = df["close"].iloc[-1]
        prev_high = df["high"].iloc[-2]
        last_high = df["high"].iloc[-1]

        score = 0
        if slope > slope_threshold:
            score += 1
        if rolling_return > rolling_return_threshold:
            score += 1
        if volatility < volatility_max:
            score += 1
        if rvol > rvol_threshold:
            score += 1
        if price > vwap.iloc[-1]:
            score += 1
        if last_close > last_open:
            score += 1
        if last_high > prev_high:
            score += 1

        if score >= min_score:
            symbol_obj["in_momentum"] = True
            symbol_obj["momentum_score"] = score
            symbol_obj["momentum_detected_at"] = datetime.now(LOCAL_TZ).isoformat()
            log(f"{symbol} IN MOMENTUM (score={score}): slope={slope:.4f}, return={rolling_return:.4f}, vol={volatility:.4f}, RVOL={rvol:.2f}", "INFO", "PolygonUtils")
        else:
            symbol_obj["in_momentum"] = False
            symbol_obj["momentum_score"] = score
            log(f"{symbol} NOT in momentum (score={score}): slope={slope:.4f}, return={rolling_return:.4f}, vol={volatility:.4f}, RVOL={rvol:.2f}", "INFO", "PolygonUtils")

        updated_symbols.append(symbol_obj)

    output_data = {
        "date": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"),
        "symbols": updated_symbols
    }
    with open(json_path, "w") as f:
        json.dump(output_data, f, indent=2)

def filter_gainers_for_momentum_score(df,
                                      slope_threshold=0.005,
                                      rolling_return_threshold=0.01,
                                      volatility_max=0.04,
                                      rvol_threshold=1.5):
    closes = df["close"]
    slope = (closes.iloc[-1] - closes.iloc[-15]) / closes.iloc[-15]
    rolling_return = closes.pct_change(15).iloc[-1]
    volatility = closes.tail(15).std() / closes.tail(15).mean()

    avg_vol = df["volume"].rolling(20).mean().iloc[-1]
    current_vol = df["volume"].iloc[-1]
    rvol = current_vol / avg_vol if avg_vol else 0

    vwap = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
    price = closes.iloc[-1]

    last_open = df["open"].iloc[-1]
    last_close = df["close"].iloc[-1]
    prev_high = df["high"].iloc[-2]
    last_high = df["high"].iloc[-1]

    score = 0
    if slope > slope_threshold:
        score += 1
    if rolling_return > rolling_return_threshold:
        score += 1
    if volatility < volatility_max:
        score += 1
    if rvol > rvol_threshold:
        score += 1
    if price > vwap.iloc[-1]:
        score += 1
    if last_close > last_open:
        score += 1
    if last_high > prev_high:
        score += 1

    return score

def load_gainers_symbols(json_path="exports/top_gainers.json"):
    """
    Loads gainers symbols from the JSON file.
    If missing or error, calls ensure_gainers_symbols() and returns its result.
    """
    if not os.path.exists(json_path):
        log(f"⚠️ No top gainers JSON at {json_path}. Attempting to refresh from Polygon.", "WARNING", "Loader")
        

    try:
        with open(json_path) as f:
            data = json.load(f)
        momentum_symbols = [s["symbol"] for s in data.get("symbols", []) if isinstance(s, dict) and s.get("in_momentum", False)]
        non_momentum_symbols = [s["symbol"] for s in data.get("symbols", []) if isinstance(s, dict) and not s.get("in_momentum", False)]
        print(f"Loaded {(momentum_symbols)} momentum symbols and {(non_momentum_symbols)} non-momentum symbols from {json_path}.")
        return momentum_symbols, non_momentum_symbols

    except Exception as e:
        log(f"❌ Error loading gainers symbols from {json_path}: {e}. Attempting to refresh from Polygon.", "ERROR", "Loader")
        

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
    top_gainers_df = get_top_gainers_polygon(limit=13)
    log(f"Top gainers dataframe:\n{top_gainers_df}", "INFO", "PolygonUtils")
    filter_gainers_for_momentum()
