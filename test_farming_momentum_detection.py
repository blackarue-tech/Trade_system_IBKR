# test_farming_momentum_detection.py
import os
import pandas as pd
from datetime import datetime
from modules.price_data import get_price_data
from backtest_engine import is_farming_momentum

SYMBOL = "HIMZ"
EXPORT_PATH = "exports/farming_debug_HIMZ.csv"
WINDOW = 15

def ensure_datetime_col(df):
    if "datetime" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["datetime"]):
        df["datetime"] = pd.to_datetime(df["datetime"])

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

SYMBOL = "HIMZ"
WINDOW = 10
SLOPE_THRESHOLD = 0.3
GREEN_PCT = 0.5

# Load price data
df = get_price_data(SYMBOL, days=1, interval="minute")
ensure_datetime_col(df)

# Initialize tracking
in_momentum = False
start_time = None
start_price = None
results = []

for i in range(WINDOW, len(df)):
    window_df = df.iloc[i - WINDOW:i].copy()
    timestamp = df.iloc[i]["datetime"]

    if is_farming_momentum(SYMBOL, window_df, WINDOW, SLOPE_THRESHOLD, GREEN_PCT):
        if not in_momentum:
            # Farming window starts
            in_momentum = True
            start_time = timestamp
            start_price = window_df["close"].iloc[-1]
    else:
        if in_momentum:
            # Farming window ends
            end_time = timestamp
            end_price = window_df["close"].iloc[-1]
            results.append({
                "symbol": SYMBOL,
                "start_time": start_time,
                "start_price": round(start_price, 2),
                "end_time": end_time,
                "end_price": round(end_price, 2),
                "duration_min": (end_time - start_time).total_seconds() / 60
            })
            in_momentum = False

# Save results
ensure_dir("exports")
df_result = pd.DataFrame(results)
df_result.to_csv(f"exports/farming_momentum_windows_{SYMBOL}.csv", index=False)

if results:
    print(f"✅ Detected {len(results)} farming momentum windows for {SYMBOL}.")
    for r in results:
        print(f"{r['start_time']} → {r['end_time']} | Entry: {r['start_price']} → Exit: {r['end_price']} | Duration: {r['duration_min']:.1f} min")
else:
    print("⚠️ No farming momentum windows detected.")
