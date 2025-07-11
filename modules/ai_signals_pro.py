#ai_signals_pro.py

import os
import pandas as pd
import numpy as np
from datetime import time, datetime, timedelta
#import datetime
from dateutil import tz
import pytz
from pytz import timezone
from modules.model_utils import add_technical_indicators, set_ema_window

TORONTO = pytz.timezone("America/Toronto")
LOG_PATH = "exports/signal_debug_log.csv"



def is_breakout(df):
    recent_high = df["high"].rolling(5).max()
    volume_spike = df["volume"] > df["volume"].rolling(5).mean() * 1.5
    breakout = df["close"] > recent_high.shift(1)
    vwap_align = df["close"] > df["vwap"]
    rsi_ok = df["rsi"] > 50
    return breakout & volume_spike & vwap_align & rsi_ok

def is_momentum(df):
    rvol = df["volume"] / df["volume"].rolling(20).mean()
    macd_positive = df["macd_hist"] > 0
    rsi_trending = df["rsi"] > 60
    vwap_support = df["close"] > df["vwap"]
    return (rvol > 2) & macd_positive & rsi_trending & vwap_support

def is_vwap_reversion(df):
    near_vwap = np.abs(df["close"] - df["vwap"]) / df["vwap"] < 0.005
    macd_conf = df["macd_hist"].diff() > 0
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
    """
    Unified scoring function for selection pipeline using:
    Breakout, Momentum, VWAP_Bounce, ORB, MACD, RSI, MA_Cross
    """
    signals = pd.DataFrame(index=df.index)
    
    # Ensure technical indicators
    df = add_technical_indicators(df)

    # Strategy conditions
    signals["Breakout"] = (df["close"] > df["high20"]) & (df["close"] > df["vwap"])
    signals["Momentum"] = (df["macd_hist"] > 0) & (df["rsi"] > 60) & (df["close"] > df["vwap"])
    signals["VWAP_Bounce"] = (abs(df["close"] - df["vwap"]) / df["vwap"] < 0.005) & (df["macd_hist"].diff() > 0)
    signals["ORB"] = (df["close"] > df["high20"]) | (df["close"] < df["low20"])
    signals["MACD"] = df["macd_hist"] > 0.01
    signals["RSI"] = df["rsi"] < 30
    signals["MA_Cross"] = df["close"] > df["ema20"]
    
    strat_cols = ["Breakout", "Momentum", "VWAP_Bounce", "ORB", "MACD", "RSI", "MA_Cross"]
    signals["confidence"] = signals[strat_cols].sum(axis=1) / len(strat_cols)
    signals["strategies"] = signals[strat_cols].apply(lambda row: [s for s, v in row.items() if v], axis=1)
    signals["signal"] = signals["confidence"] > 0.5
    
    return signals
def append_signal_log(symbol, timestamp, confidence, strategies):
    row = {
        "symbol": symbol,
        "timestamp": timestamp,
        "confidence": confidence,
        "strategies": ", ".join(strategies)
    }
    df = pd.DataFrame([row])
    #if not os.path.exists(LOG_PATH):
        #df.to_csv(LOG_PATH, index=False)
    #else:
        #df.to_csv(LOG_PATH, mode='a', header=False, index=False)
        
def get_strategy_signals(symbol, df, min_confidence=0.5, debug=False, config=None):
    """
    Unified execution signal generation using the same strategies as score_signals
    """
    config = config or {}
    ema_window = config.get("ema_window", 20)
    set_ema_window(ema_window)
    df = add_technical_indicators(df)
    
    # Ensure datetime
    if isinstance(df.index, pd.DatetimeIndex) and "datetime" not in df.columns:
        df = df.copy()
        df["datetime"] = df.index
    
    signals = []
    for i in range(len(df)):
        row = df.iloc[i]
        timestamp = pd.Timestamp(row["datetime"]).tz_localize("UTC").tz_convert(TORONTO) if not pd.Timestamp(row["datetime"]).tzinfo else pd.Timestamp(row["datetime"]).tz_convert(TORONTO)

        confidence = 0.0
        strategies = []

        if row.get("macd_hist", 0) > 0.01:
            confidence += 0.15
            strategies.append("MACD")

        if row.get("rsi", 50) < 30:
            confidence += 0.15
            strategies.append("RSI")

        if row.get("close", 0) > row.get("high20", 0):
            confidence += 0.15
            strategies.append("Breakout")

        if row.get("macd_hist", 0) > 0 and row.get("rsi", 50) > 60 and row.get("close", 0) > row.get("vwap", 0):
            confidence += 0.15
            strategies.append("Momentum")

        if abs(row.get("close", 0) - row.get("vwap", 0)) / row.get("vwap", 1) < 0.005 and (df["macd_hist"].iloc[i] - df["macd_hist"].iloc[i-1] if i > 0 else 0) > 0:
            confidence += 0.15
            strategies.append("VWAP_Bounce")

        if row.get("close", 0) > row.get(f"ema{ema_window}", 0):
            confidence += 0.1
            strategies.append("MA_Cross")

        if (row.get("close", 0) > row.get("high20", 0)) or (row.get("close", 0) < row.get("low20", 0)):
            confidence += 0.05
            strategies.append("ORB")

        if confidence >= min_confidence:
            signals.append({
                "symbol": symbol,
                "confidence": round(confidence, 3),
                "strategies": strategies,
                "index": i,
                "datetime": timestamp.isoformat()
            })
    
    return pd.DataFrame(signals)