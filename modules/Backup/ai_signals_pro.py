
import pandas as pd
import numpy as np
from datetime import time

def is_breakout(df):
    recent_high = df["high"].rolling(5).max()
    volume_spike = df["volume"] > df["volume"].rolling(5).mean() * 1.5
    breakout = df["close"] > recent_high.shift(1)
    vwap_align = df["close"] > df["VWAP"]
    rsi_ok = df["RSI"] > 50
    return breakout & volume_spike & vwap_align & rsi_ok

def is_momentum(df):
    rvol = df["volume"] / df["volume"].rolling(20).mean()
    macd_positive = df["MACD_hist"] > 0
    rsi_trending = df["RSI"] > 60
    vwap_support = df["close"] > df["VWAP"]
    return (rvol > 2) & macd_positive & rsi_trending & vwap_support

def is_vwap_reversion(df):
    near_vwap = np.abs(df["close"] - df["VWAP"]) / df["VWAP"] < 0.005
    macd_conf = df["MACD_hist"].diff() > 0
    return near_vwap & macd_conf

def is_orb(df, open_range_minutes=15):
    open_time = df.index[0].replace(hour=9, minute=30)
    orb_end = open_time + pd.Timedelta(minutes=open_range_minutes)
    opening_range = df[(df.index >= open_time) & (df.index <= orb_end)]
    if opening_range.empty:
        return pd.Series([False] * len(df), index=df.index)
    high = opening_range["high"].max()
    low = opening_range["low"].min()
    breakout = (df["close"] > high) | (df["close"] < low)
    return breakout

def score_signals(df):
    signals = pd.DataFrame(index=df.index)
    signals["Breakout"] = is_breakout(df)
    signals["Momentum"] = is_momentum(df)
    signals["VWAP_Bounce"] = is_vwap_reversion(df)
    signals["ORB"] = is_orb(df)

    signals["confidence"] = (
        signals[["Breakout", "Momentum", "VWAP_Bounce", "ORB"]].sum(axis=1) / 4.0
    )
    signals["strategies"] = signals[["Breakout", "Momentum", "VWAP_Bounce", "ORB"]].apply(
        lambda row: [s for s, v in row.items() if v], axis=1
    )
    signals["signal"] = signals["confidence"] > 0.5
    return signals

def get_strategy_signals(symbol, df, min_confidence=0.3, debug=False):
    df = df.copy()
    required_cols = ["high", "low", "close", "volume", "VWAP", "RSI", "MACD_hist"]
    if not all(col in df.columns for col in required_cols):
        if debug:
            print(f"⚠️ Missing columns for {symbol}: {[col for col in required_cols if col not in df.columns]}")
        return pd.DataFrame()

    signals = score_signals(df)
    result = signals[signals["signal"] & (signals["confidence"] >= min_confidence)]

    if debug:
        print(f"🔍 {symbol} - {len(result)} signals found with confidence >= {min_confidence}")
    return result
