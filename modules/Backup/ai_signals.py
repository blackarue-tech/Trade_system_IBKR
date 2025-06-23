# ai_signals.py

import pandas as pd
import numpy as np
import json
import os
import matplotlib.pyplot as plt
import plotly.express as px
import streamlit as st
import contextlib
import shutil
from datetime import datetime
import glob
from utils.model_utils import apply_context_penalty

STATS_FILE = "exports/ai_strategy_stats.json"

# Load strategy stats if exists
if os.path.exists(STATS_FILE) and os.path.getsize(STATS_FILE) > 0:
    with open(STATS_FILE, "r") as f:
        strategy_stats = json.load(f)
else:
    strategy_stats = {
            "per_symbol": {},
            "pnl_history": [],
            "rolling_sharpe": 0,
            "drawdown_streak": 0,
            "risk_pct": 0.005,
            "total_trades": 0,
            "wins": 0,
            "history": []
        }

def save_strategy_stats_with_backup(filepath="exports/ai_strategy_stats.json"):
    os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
    with open(STATS_FILE, "w") as f:
        json.dump(strategy_stats, f, indent=2)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = filepath.replace(".json", f"_{timestamp}.bak.json")
    shutil.copy(filepath, backup_path)

    base_name = filepath.replace(".json", "")
    backups = sorted(glob.glob(f"{base_name}_*.bak.json"), reverse=True)
    for old_backup in backups[5:]:
        os.remove(old_backup)

def save_strategy_stats():
    os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
    with open(STATS_FILE, "w") as f:
        json.dump(strategy_stats, f, indent=2)

def visualize_strategy_performance_tab():
    st.header("🧠 Strategy Learning Performance")
    history = strategy_stats.get("history", [])
    if not history:
        st.warning("No history to display.")
        return
    df = pd.DataFrame(history)
    df["step"] = range(1, len(df) + 1)
    fig = px.line(df, x="step", y=["win_rate", "risk_pct"], markers=True,
                  title="Strategy Learning Progress",
                  labels={"value": "Metric", "step": "Trade Count", "variable": "Metric"})
    st.plotly_chart(fig, use_container_width=True)

def get_strategy_signals(symbol, df, min_confidence=0.0, real_trade_logs=None):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / (avg_loss + 1e-6)
    df['RSI'] = 100 - (100 / (1 + rs))

    signals = []

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        direction = None
        confidence = 0.0
        strategies_used = []
        confidence_breakdown = {}

        # MA crossover
        if prev['MA_fast'] < prev['MA_slow'] and curr['MA_fast'] > curr['MA_slow']:
            direction = "buy"
            confidence += 0.3
            confidence_breakdown["MA_Crossover"] = 0.3
            strategies_used.append("MA_Crossover")
        elif prev['MA_fast'] > prev['MA_slow'] and curr['MA_fast'] < curr['MA_slow']:
            direction = "sell"
            confidence += 0.3
            confidence_breakdown["MA_Crossover"] = 0.3
            strategies_used.append("MA_Crossover")

        # RSI (more trades triggered by increasing threshold)
        if curr['RSI'] < 45:
            if direction in [None, "buy"]:
                direction = "buy"
                confidence += 0.25
                confidence_breakdown["RSI"] = 0.25
                strategies_used.append("RSI")
        elif curr['RSI'] > 65:
            if direction in [None, "sell"]:
                direction = "sell"
                confidence += 0.25
                confidence_breakdown["RSI"] = 0.25
                strategies_used.append("RSI")

        # Breakout logic
        if curr['close'] > curr['high20'] * 1.005:
            if direction in [None, "buy"]:
                direction = "buy"
                confidence += 0.25
                confidence_breakdown["Breakout"] = 0.25
                strategies_used.append("Breakout")

        # Fallback low-confidence signal (more trades even if weak)
        if direction is None and curr['RSI'] < 55:
            direction = "buy"
            confidence = 0.1
            confidence_breakdown["RSI_soft"] = 0.1
            strategies_used.append("RSI_soft")

        if direction and confidence >= min_confidence:
            # Context modifiers
            rsi = curr.get("RSI", 50)
            volatility = df["close"].rolling(window=20).std().iloc[i]
            macd_hist = curr.get("macd_hist", 0)
            for strat in strategies_used:
                confidence = apply_context_penalty(strat, rsi, volatility, macd_hist, confidence)

            confidence = round(min(confidence, 1.0), 2)

            # Log the signal
            signals.append({
                "datetime": curr.name,
                "price": curr["close"],
                "signal": direction,
                "confidence": confidence,
                "confidence_breakdown": confidence_breakdown,
                "strategies_used": strategies_used,
                "symbol": symbol,
                "volatility_band": "high" if volatility > 3 else "medium" if volatility > 1 else "low",
                "win_rate": 0.5,
                "risk_pct": 0.005
            })

            print(f"{symbol} @ {curr.name}: Confidence={confidence}, Strategies={strategies_used}")

    return pd.DataFrame(signals)
