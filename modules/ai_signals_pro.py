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
def append_signal_log(symbol, timestamp, confidence, strategies):
    row = {
        "symbol": symbol,
        "timestamp": timestamp,
        "confidence": confidence,
        "strategies": ", ".join(strategies)
    }
    df = pd.DataFrame([row])
    if not os.path.exists(LOG_PATH):
        df.to_csv(LOG_PATH, index=False)
    else:
        df.to_csv(LOG_PATH, mode='a', header=False, index=False)
        
def get_strategy_signals(symbol, df, min_confidence=0.5, debug=False, config=None):
    #print(f"📊 Running signal check for {symbol} - total rows: {len(df)}")
    config = config or {}
    rsi_threshold = config.get("rsi_threshold", 30)
    breakout_pct = config.get("breakout_pct", 0.02)
    macd_hist_min = config.get("macd_hist_min", 0.01)
    ema_window = config.get("ema_window", 20)
    set_ema_window(ema_window)
    TORONTO = pytz.timezone("America/Toronto")
    now_local = datetime.now(TORONTO)
    df = add_technical_indicators(df)
        
    
    # ✅ Ensure datetime is a column
    if isinstance(df.index, pd.DatetimeIndex) and "datetime" not in df.columns:
        df = df.copy()
        df["datetime"] = df.index

   
    signals = []
    debug_log_rows = []

    for i in range(len(df)):
        row = df.iloc[i]

        # Prevent failure from missing datetime early
        if "datetime" not in row:
            print(f"⚠️ Missing 'datetime' in signals DataFrame for {symbol}. Columns: {df.columns.tolist()}")
            continue

        timestamp = pd.Timestamp(row["datetime"])
        if not timestamp.tzinfo:
            timestamp = timestamp.tz_localize("UTC").tz_convert(TORONTO)
        else:
            timestamp = timestamp.tz_convert(TORONTO)

        confidence = 0.0
        strategies = []
        debug_info = {
            "datetime": row["datetime"],
            "close": row.get("close"),
            "vwap": row.get("vwap"),
            "rsi": row.get("rsi"),
            "macd_hist": row.get("macd_hist"),
            "ema20": row.get("ema20"),
            "strategies_fired": [],
        }

        # Strategy: MACD
        if not pd.isna(row.get("macd_hist")) and row["macd_hist"] > macd_hist_min:
            confidence += 0.3
            strategies.append("MACD")
            debug_info["strategies_fired"].append("MACD")

        # Strategy: RSI
        if not pd.isna(row.get("rsi")) and row["rsi"] < rsi_threshold:
            confidence += 0.3
            strategies.append("RSI")
            debug_info["strategies_fired"].append("RSI")

        # Strategy: Breakout
        if i >= 20:
            recent_high = df["high"].iloc[i - 20:i + 1].max()
            if row["close"] > recent_high * (1 + breakout_pct):
                confidence += 0.3
                strategies.append("Breakout")
                debug_info["strategies_fired"].append("Breakout")

        # Strategy: MA Crossover
        ema_col = f"ema{ema_window}"
        if not pd.isna(row.get(ema_col)) and row["close"] > row[ema_col]:
            confidence += 0.1
            strategies.append("MA_Cross")
            debug_info["strategies_fired"].append("MA_Cross")

        debug_info["confidence"] = confidence
        debug_info["strategies"] = ",".join(strategies)
        debug_log_rows.append(debug_info)

        if confidence >= min_confidence:
            #print(f"✅ {symbol} @ {timestamp}: conf={confidence:.2f} from {strategies}")
            append_signal_log(symbol, timestamp.isoformat(), confidence, strategies)
            signals.append({
                "symbol": symbol,
                "confidence": confidence,
                "strategies": strategies,
                "index": i,
                "datetime": timestamp.isoformat()
            })

    # ✅ Export debug CSV after loop
    if debug and debug_log_rows:
        debug_df = pd.DataFrame(debug_log_rows)
        timestamp_str = now_local.strftime("%Y%m%d_%H%M%S")
        debug_df.to_csv(f"exports/debug_signals_{timestamp_str}.csv", index=False)

    return pd.DataFrame(signals)