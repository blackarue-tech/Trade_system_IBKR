# model_utils.py

import os
import pandas as pd
import joblib
import numpy as np
from datetime import datetime, timedelta
from price_data import get_price_data
import warnings
from sklearn.exceptions import DataConversionWarning

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DataConversionWarning)

FEATURE_COLUMNS = [
    "MA20", "STD20", "UpperBB", "LowerBB", "bb_width",
    "EMA12", "EMA26", "MACD", "Signal", "macd_hist",
    "atr", "rsi", "vwap", "volatility"
]

def save_model(model, filename="stock_selector_model.pkl"):
    os.makedirs("exports", exist_ok=True)
    path = os.path.join("exports", filename)
    joblib.dump(model, path)
    print(f"✅ Model saved to {path}")

def load_model(filename="stock_selector_model.pkl"):
    path = os.path.join("exports", filename)
    if os.path.exists(path):
        print(f"✅ Model loaded from {path}")
        return joblib.load(path)
    print("⚠️ Model file not found.")
    return None

def get_recent_daily_data(symbol_id, days=90):
    end = datetime.now()
    start = end - timedelta(days=days)
    return get_price_data(symbol_id, days=days, interval="OneDay")


def add_technical_indicators(df):
    if "datetime" not in df.columns and "start" in df.columns:
        df["datetime"] = pd.to_datetime(df["start"], errors="coerce", utc=True)

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
        if df["datetime"].notna().sum() > 0:
            df = df.set_index("datetime")
            df = df.sort_index()
        else:
            print("⚠️ datetime column exists but is all NaT, skipping index set.")
    else:
        print("⚠️ 'datetime' column missing from DataFrame before technical indicators.")

    df["MA_fast"] = df["close"].rolling(window=9).mean()
    df["MA_slow"] = df["close"].rolling(window=21).mean()
    df["high20"] = df["high"].rolling(window=20).max()
    df["low20"] = df["low"].rolling(window=20).min()
    df["MA20"] = df["close"].rolling(window=20).mean()
    df["STD20"] = df["close"].rolling(window=20).std()
    df["UpperBB"] = df["MA20"] + 2 * df["STD20"]
    df["LowerBB"] = df["MA20"] - 2 * df["STD20"]
    df["bb_width"] = df["UpperBB"] - df["LowerBB"]
    df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["EMA26"] = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = df["EMA12"] - df["EMA26"]
    df["Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["MACD"] - df["Signal"]

    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    df["high_low"] = df["high"] - df["low"]
    df["high_close"] = (df["high"] - df["close"].shift()).abs()
    df["low_close"] = (df["low"] - df["close"].shift()).abs()
    df["tr"] = df[["high_low", "high_close", "low_close"]].max(axis=1)
    df["atr"] = df["tr"].rolling(window=14).mean()

    df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
    df["log_return"] = np.log(df["close"] / df["close"].shift(1))
    df["volatility"] = df["log_return"].rolling(window=14).std()

    return df

def apply_context_penalty(strategy, rsi, volatility, macd_hist, confidence):
    penalty = 0.0
    if strategy == "MA_Crossover" and volatility > 0.03:
        penalty += 0.1
    if strategy == "RSI" and (rsi > 70 or rsi < 30):
        penalty += 0.05
    if strategy == "Breakout" and macd_hist < 0:
        penalty += 0.1

    return max(confidence - penalty, 0.0)

def compute_signal_quality(trades_df):
    quality_map = {}
    if trades_df.empty:
        return quality_map

    for strategy in trades_df["strategy"].dropna().unique():
        strat_df = trades_df[trades_df["strategy"] == strategy]
        win_rate = (strat_df["pnl"] > 0).mean()
        quality_map[strategy] = round(win_rate, 3)

    return quality_map

def generate_training_data(df, forward_days=4, return_threshold=0.01):
    df = add_technical_indicators(df)
    try:
        df["future_return"] = df["close"].shift(-forward_days) / df["close"] - 1
        df["label"] = (df["future_return"] > return_threshold).astype(int)
    except Exception as e:
        print(f"❌ Label generation failed: {e}")
    return df.dropna().copy()

def extract_features(df):
    try:
        if isinstance(df, list):
            df = pd.DataFrame(df)
        if df is None or df.empty or len(df) < 10:
            raise ValueError(f"Not enough data to extract features: got {len(df)} rows")
        
        df = add_technical_indicators(df)

        df["avg_close"] = df["close"].rolling(window=10).mean()
        df["volatility"] = df["close"].rolling(window=10).std()
        df["momentum"] = df["close"] - df["close"].shift(10)
        df["volume_avg"] = df["volume"].rolling(window=10).mean()
        df["delta_1d"] = df["close"].pct_change(periods=1)
        df["delta_5d"] = df["close"].pct_change(periods=5)

        # Debug preview
        # print(f"🧮 Feature input preview: close={df['close'].tail(10).tolist()}")

        return df

    except Exception as e:
        print(f"❌ Feature extraction failed: {e}")
        return None