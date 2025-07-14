# validate_momentum_conditions.py

import pandas as pd
import requests
import time
from datetime import datetime, timedelta
import numpy as np
import os
from pytz import timezone
from dotenv import load_dotenv


load_dotenv()

# Polygon API key via secrets or environment
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
if not POLYGON_API_KEY:
    raise ValueError("Missing Polygon API key. Set POLYGON_API_KEY as environment variable.")

BASE_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from_date}/{to_date}?adjusted=true&sort=asc&limit=50000&apiKey=" + POLYGON_API_KEY

# Timezone consistency
LOCAL_TZ = timezone("America/Toronto")
CONTROL_PATH = "inputs/momentum_trades_symbol_datetime.csv"
RESULT_PATH = "inputs/momentum_trades_symbolls_result.csv"
def fetch_polygon_candles(symbol, start, end, timespan="minute", multiplier=1):
    url = BASE_URL.format(
        symbol=symbol,
        multiplier=multiplier,
        timespan=timespan,
        from_date=start.strftime("%Y-%m-%d"),
        to_date=end.strftime("%Y-%m-%d")
    )
    response = requests.get(url)
    data = response.json()
    if 'results' not in data:
        print(f"No data for {symbol}")
        return pd.DataFrame()
    df = pd.DataFrame(data['results'])
    df['t'] = pd.to_datetime(df['t'], unit='ms').dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ)
    df = df.rename(columns={
        't': 'datetime', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'
    })
    return df[['datetime', 'open', 'high', 'low', 'close', 'volume']]

def is_momentum_confirmed(df, entry_time):
    """
    Score-based momentum confirmation:
    - 1pt: Last candle is green
    - 1pt: Previous candle is green
    - 1pt: Last candle has higher high
    - 1pt: RVOL > 1.5
    Momentum confirmed if score >= 3
    """
    if df.empty:
        return False, "No candle data"

    df = df[df['datetime'] <= entry_time].copy()
    if len(df) < 2:
        return False, "Not enough candles"

    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0
    notes = []

    if last['close'] > last['open']:
        score += 1
        notes.append("last green")
    else:
        notes.append("last red")

    if prev['close'] > prev['open']:
        score += 1
        notes.append("prev green")
    else:
        notes.append("prev red")

    if last['high'] > prev['high']:
        score += 1
        notes.append("higher high")
    else:
        notes.append("no breakout")

    rvol = last['volume'] / df['volume'].rolling(20).mean().iloc[-1]
    notes.append(f"RVOL={rvol:.2f}")
    if rvol > 1.5:
        score += 1

    momentum = score >= 3
    notes.append(f"score={score}")
    return momentum, ", ".join(notes)


def main():
    df = pd.read_csv(CONTROL_PATH)
    results = []
    for idx, row in df.iterrows():
        symbol = row['symbol']
        dt = pd.to_datetime(row['datetime'])
        if dt.tzinfo is None:
            dt = dt.tz_localize(LOCAL_TZ)
        else:
            dt = dt.tz_convert(LOCAL_TZ)
        start_time = dt - timedelta(minutes=15)
        candles = fetch_polygon_candles(symbol, start_time, dt + timedelta(minutes=1))
        valid, notes = is_momentum_confirmed(candles, dt)
        results.append({
            'symbol': symbol,
            'datetime': dt.isoformat(),
            'momentum_confirmed': valid,
            'notes': notes
        })
        print(f"Checked {symbol} at {dt}: {'✅' if valid else '❌'} | {notes}")
        time.sleep(0.3)  # to avoid rate limits

    pd.DataFrame(results).to_csv(RESULT_PATH, index=False)
    print("✅ Validation complete. Results saved to momentum_trades_symbolls_result.csv")

if __name__ == "__main__":
    main()
