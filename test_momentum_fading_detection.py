# test_momentum_fading_detection.py
import os
import pandas as pd
from datetime import datetime
from modules.price_data import get_price_data
from backtest_engine import is_momentum_fading

SYMBOL = "HIMZ"
LOOKBACK = 5
EXPORT_PATH = f"exports/momentum_fading_windows_{SYMBOL}.csv"

def ensure_datetime_col(df):
    if "datetime" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["datetime"]):
        df["datetime"] = pd.to_datetime(df["datetime"])

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

# Load price data
df = get_price_data(SYMBOL, days=1, interval="minute")
ensure_datetime_col(df)

fading_windows = []
in_fade = False
start_time = None
start_price = None

for i in range(LOOKBACK + 1, len(df)):
    window_df = df.iloc[i - LOOKBACK - 1:i].copy()
    timestamp = df.iloc[i]["datetime"]

    if is_momentum_fading(SYMBOL, window_df, lookback=LOOKBACK):
        if not in_fade:
            in_fade = True
            start_time = timestamp
            start_price = window_df["close"].iloc[-1]
    else:
        if in_fade:
            end_time = timestamp
            end_price = window_df["close"].iloc[-1]
            fading_windows.append({
                "symbol": SYMBOL,
                "start_time": start_time,
                "start_price": round(start_price, 2),
                "end_time": end_time,
                "end_price": round(end_price, 2),
                "duration_min": (end_time - start_time).total_seconds() / 60
            })
            in_fade = False

ensure_dir("exports")
df_result = pd.DataFrame(fading_windows)
df_result.to_csv(EXPORT_PATH, index=False)

if fading_windows:
    print(f"✅ Detected {len(fading_windows)} fading momentum windows for {SYMBOL}.")
    for r in fading_windows:
        print(f"{r['start_time']} → {r['end_time']} | Entry: {r['start_price']} → Exit: {r['end_price']} | Duration: {r['duration_min']:.1f} min")
else:
    print("⚠️ No fading momentum windows detected.")
