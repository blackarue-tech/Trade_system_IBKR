# polygon_top_gainers.py - fetches top 100 gainers with price momentum, every 5 min

import os
import time
import json
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from modules.log_utils import log, fail_safe
load_dotenv()

API_KEY = os.getenv("POLYGON_API_KEY")
if not API_KEY:
    log("⚠️ POLYGON_API_KEY not set. Check your .env file.", "WARNING", "PolygonUtils")
else:
    log("✅ POLYGON_API_KEY loaded successfully.", "INFO", "PolygonUtils")
    
LOG_PATH = "exports/polygon_top_gainers_log.json"
MOMENTUM_THRESHOLD = 5  # percent change
PRICE_THRESHOLD = 50
LOOKBACK_MINUTES = 30
SCAN_INTERVAL = 60  # 5 min

def fetch_top_gainers(limit=100):
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={API_KEY}&limit={limit}"
    response = requests.get(url)
    data = response.json()
    return data.get("tickers", [])


def load_log():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            return json.load(f)
    return {}


def save_log(log):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def detect_momentum():
    now = datetime.utcnow()
    tickers = fetch_top_gainers()
    log = load_log()

    qualified = []

    for ticker in tickers:
        symbol = ticker.get("ticker")
        price = ticker.get("lastTrade", {}).get("p")
        if price is None or price < PRICE_THRESHOLD:
            continue

        if symbol not in log:
            log[symbol] = []

        log[symbol].append({
            "price": price,
            "timestamp": now.isoformat()
        })

        # Filter old records
        log[symbol] = [r for r in log[symbol] if datetime.fromisoformat(r["timestamp"]) >= now - timedelta(minutes=LOOKBACK_MINUTES)]

        if len(log[symbol]) >= 2:
            first_price = log[symbol][0]["price"]
            pct_change = (price - first_price) / first_price * 100
            if pct_change >= MOMENTUM_THRESHOLD:
                qualified.append((symbol, price, round(pct_change, 2)))

    save_log(log)
    return qualified


if __name__ == "__main__":
    while True:
        try:
            momentum_symbols = detect_momentum()
            print(f"✅ [{datetime.now()}] Top Gainers with Momentum > {MOMENTUM_THRESHOLD}% and Price > ${PRICE_THRESHOLD}:")
            for sym, price, pct in momentum_symbols:
                print(f"{sym} - ${price} ({pct}%)")
            print(f"⏱️ Sleeping {SCAN_INTERVAL}s before next scan...\n")
        except Exception as e:
            print(f"❌ Error during scan: {e}")
        time.sleep(SCAN_INTERVAL)

