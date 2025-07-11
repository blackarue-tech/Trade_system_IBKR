#trade_utils.py

import streamlit as st
import os
import json
import shutil
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime,timedelta,timezone,time
import random
import requests
import holidays
from urllib.parse import urlencode
from questrade_api import get_access_token, fetch_candles,get_quote
from ai_signals import get_strategy_signals
from utils.model_utils import add_technical_indicators
from utils.price_data import get_price_data
from utils.context_utils import load_context_features
from utils.Wallet_Utils import load_wallet_balance, debit_wallet_balance, get_wallet_available_capital

LOG_DIR = "exports/trade_logs"
LIVE_LOG_PATH = "exports/live_trades.json"
TOP_SIGNALS_PATH = "exports/top_confidence_signals.json"
CONTEXT_FEATURES_PATH = "exports/context_features.json"
CONFIDENCE_THRESHOLD = 0.3 # you can make this dynamic later via Streamlit config
SKIPPED_TRADES_PATH = "exports/skipped_trades.json"
WALLET_FILE = "exports/wallet.json"

def get_live_price_data(symbol, periods=60):
    try:
        # Load today's AI selection JSON
        with open("exports/daily_ai_selection_scores.json", "r") as f:
            selection_data = json.load(f)

        today = datetime.now().strftime("%Y-%m-%d")
        day_data = selection_data.get(today, [])
        match = next((entry for entry in day_data if entry["symbol"] == symbol), None)

        if not match or "symbolId" not in match:
            st.warning(f"⚠️ {symbol} not found in AI selection or missing symbolId.")
            return pd.DataFrame()

        symbol_id = match["symbolId"]
        access_token, api_server = get_access_token()

        # Try intraday candles first
        end_time = datetime.now().astimezone()
        start_time = end_time - timedelta(hours=6)
        #st.write(f"🔍 Requesting OneMinute candles for {symbol} from {start_time} to {end_time}")

        df = fetch_candles(symbol_id, start_time, end_time, access_token, api_server)
        if df is not None and "start" in df.columns:
            df["datetime"] = pd.to_datetime(df["start"], utc=True)

        if df is None or df.empty:
            st.warning("⚠️ No intraday data found. Trying OneDay fallback...")
            df = fetch_candles(symbol_id, None, None, access_token, api_server, days=5)
            if df is not None and "start" in df.columns:
                df["datetime"] = pd.to_datetime(df["start"], utc=True)

        if df is None or df.empty or "datetime" not in df.columns:
            st.error("❌ No price data available after fallback.")
            return pd.DataFrame()

        df = df.tail(periods).copy()

        # Rolling indicators with safety checks
        if len(df) >= 5:
            df["MA_fast"] = df["close"].rolling(window=5).mean()
        if len(df) >= 15:
            df["MA_slow"] = df["close"].rolling(window=15).mean()
        if len(df) >= 20:
            df["high20"] = df["close"].rolling(window=20).max()

        if df["close"].isna().all():
            st.warning("⚠️ Not enough price data to chart.")
            return pd.DataFrame()

        if df.dropna(subset=["close"]).empty:
            st.warning("⚠️ Indicators couldn't be calculated due to insufficient history. Showing raw prices only.")
            return df[["datetime", "close"]]

        st.write(f"📊 Loaded {len(df)} candles for {symbol}")
        return df.dropna()

    except Exception as e:
        st.error(f"❌ Error loading live price data: {e}")
        return pd.DataFrame()


def log_skipped_trade(symbol, reason, context=None):
    today = datetime.now().date().isoformat()
    entry = {
        "symbol": symbol,
        "reason": reason,
        "time": datetime.now().isoformat(),
    }
    if context:
        entry.update(context)

    if os.path.exists(SKIPPED_TRADES_PATH):
        with open(SKIPPED_TRADES_PATH, "r") as f:
            data = json.load(f)
    else:
        data = {}

    data.setdefault(today, []).append(entry)

    with open(SKIPPED_TRADES_PATH, "w") as f:
        json.dump(data, f, indent=2)
def load_trade_log(date_str):
    path = os.path.join(LOG_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)

def load_live_trades():
    os.makedirs(os.path.dirname(LIVE_LOG_PATH), exist_ok=True)

    # If file does not exist, create a fresh one
    if not os.path.exists(LIVE_LOG_PATH):
        with open(LIVE_LOG_PATH, "w") as f:
            json.dump({"trades": []}, f)

    # Try loading the live trades
    try:
        with open(LIVE_LOG_PATH, "r") as f:
            data = json.load(f)
        print(f"📥 Loaded {len(data.get('trades', []))} trades")
        # ✅ Backup logic (only once per session)
        if "live_backup_done" not in st.session_state:
            backup_dir = "exports/live_trades_backups"
            os.makedirs(backup_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(backup_dir, f"live_trades_backup_{timestamp}.json")
            shutil.copy2(LIVE_LOG_PATH, backup_path)
            st.session_state["live_backup_done"] = True
            print(f"🛡️ Backup created at {backup_path}")

            # ✅ Keep only the 5 most recent backups
            backups = sorted(
                [f for f in os.listdir(backup_dir) if f.endswith(".json")],
                key=lambda name: os.path.getmtime(os.path.join(backup_dir, name)),
                reverse=True
            )
            for old_file in backups[5:]:
                old_path = os.path.join(backup_dir, old_file)
                os.remove(old_path)
                print(f"🧹 Removed old backup: {old_path}")

        return data

    except (json.JSONDecodeError, ValueError) as e:
        corrupted_path = LIVE_LOG_PATH.replace(".json", f"_corrupted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        shutil.move(LIVE_LOG_PATH, corrupted_path)
        print(f"❌ Corrupted JSON detected — moved to {corrupted_path}")
        st.warning("⚠️ Corrupted trade log file detected. A backup has been saved and the file was reset.")
        with open(LIVE_LOG_PATH, "w") as f:
            json.dump({"trades": []}, f)
        return {"trades": []}
    
def save_live_trades(new_data):
    path = "exports/live_trades.json"

    # Load current data and compare lengths
    try:
        with open(path, "r") as f:
            existing_data = json.load(f)
            existing_trades = existing_data.get("trades", [])
            new_trades = new_data.get("trades", [])

            if len(new_trades) < len(existing_trades):
                print("🚫 Prevented overwrite with older version (fewer trades).")
                return  # 🔒 Prevent accidental rollback
    except Exception as e:
        print(f"⚠️ Could not compare previous data: {e}")

    with open(path, "w") as f:
        json.dump(new_data, f, indent=2)
        print(f"💾 Saving {len(new_data.get('trades', []))} trades to live_trades.json")
    

# Utility: classify time of day from timestamp
def classify_time_of_day(dt):
    hour = dt.hour
    if hour < 11:
        return "open"
    elif hour < 14:
        return "midday"
    else:
        return "close"

def update_exit_prices():
    data = load_live_trades()
    updated = False
    now = datetime.now()

    for trade in data["trades"]:
        if trade.get("exit_price") is not None:
            continue

        exit_time = pd.to_datetime(trade["exit_time"])
        if now < exit_time:
            continue

        symbol = trade["symbol"]
        symbol_id = trade.get("symbolId")
        if not symbol_id:
            print(f"⚠️ Missing symbolId for {symbol}, skipping.")
            continue

        access_token, api_server = get_access_token()
        quote = get_quote(symbol, symbol_id, access_token, api_server)

        exit_price = None
        exit_price_source = None

        if isinstance(quote, dict):
            bid = quote.get("bid")
            last = quote.get("last")
            if isinstance(bid, (int, float)) and bid > 0:
                exit_price = bid
                exit_price_source = "bidPrice"
            elif isinstance(last, (int, float)) and last > 0:
                exit_price = last
                exit_price_source = "lastTradePrice"

        if not exit_price:
            df = get_price_data(symbol_id, days=1, interval="OneDay")
            if df is not None and not df.empty:
                exit_price = float(df["close"].iloc[-1])
                exit_price_source = "candle_close"

        if not exit_price:
            print(f"⚠️ No exit price for {symbol}, skipping.")
            continue

        entry = trade["entry_price"]
        shares = trade.get("shares", 1)
        # Guard against missing or invalid entry price
        if not isinstance(entry, (int, float)) or entry <= 0:
            print(f"⚠️ Invalid entry_price for {trade['symbol']}, skipping.")
            continue
        pnl = round((exit_price - entry) * shares, 2)

        trade["exit_price"] = round(exit_price, 2)
        trade["exit_price_source"] = exit_price_source
        trade["pnl"] = pnl
        updated = True

        print(f"✅ Exited {symbol} at {exit_price} → PnL: {pnl}")

    if updated:
        save_live_trades(data)
        print("✅ Exit prices updated.")
    else:
        print("ℹ️ No trades needed exit update.")

def append_live_trade(
    symbol, symbol_id, 
    base_capital=1000.0, 
    risk_scaling="Confidence-Weighted", 
    confidence_multiplier=2.0,
    confidence_threshold=0.3,
    wallet_path="exports/wallet.json"
):
    now = datetime.now()
    print(f"\n🟡 Starting trade append for {symbol}...")

    context = load_context_features(symbol)
    if not context:
        log_skipped_trade(symbol, "no_context_features")
        print(f"⚠️ No context found for {symbol}, skipping.")
        return

    df = get_price_data(symbol_id, days=30, interval="OneDay")
    if df is None or df.empty:
        log_skipped_trade(symbol, "no_price_data")
        print(f"⚠️ No price data found for {symbol}")
        return

    df = add_technical_indicators(df)

    try:
        signal_df = get_strategy_signals(symbol, df)
    except Exception as e:
        log_skipped_trade(symbol, "signal_generation_error", {"error": str(e)})
        print(f"❌ Error computing strategy signals for {symbol}: {e}")
        return

    if signal_df is None or signal_df.empty:
        log_skipped_trade(symbol, "no_valid_signals")
        print(f"⚠️ No valid strategy signal for {symbol}")
        return

    signal = signal_df.iloc[-1]
    confidence = float(signal.get("confidence", 0))
    print(f"📊 Confidence for {symbol}: {confidence:.2f}")
    if confidence < confidence_threshold:
        log_skipped_trade(symbol, "confidence_below_threshold", {
            "confidence": confidence,
            "threshold": confidence_threshold
        })
        print(f"⚠️ Skipping trade for {symbol}: confidence {confidence:.2f} below threshold {confidence_threshold}")
        return

    wallet_balance = get_wallet_available_capital(wallet_path)
    proposed_capital = base_capital * min(confidence * confidence_multiplier, confidence_multiplier)
    proposed_capital = min(proposed_capital, wallet_balance)

    access_token, api_server = get_access_token()
    quote = get_quote(symbol, symbol_id, access_token, api_server)
    entry_price = None
    quote_source = None

    if isinstance(quote, dict) and quote:
        ask_price = quote.get("ask")
        last_price = quote.get("last")
        if isinstance(ask_price, (int, float)) and ask_price > 0:
            entry_price = ask_price
            quote_source = "askPrice"
        elif isinstance(last_price, (int, float)) and last_price > 0:
            entry_price = last_price
            quote_source = "lastTradePrice"

    if not entry_price:
        try:
            entry_price = float(df["close"].iloc[-1])
            quote_source = "candle_close"
            print(f"⚠️ Quote fallback used for {symbol}, close={entry_price}")
        except:
            log_skipped_trade(symbol, "quote_error_or_zero_price")
            print(f"❌ No fallback price available for {symbol}. Skipping.")
            return
    else:
        print(f"✅ Logged trade for {symbol} using {quote_source}: {entry_price}")

    # 🔐 Calculate shares and cap it to 2 max
    shares = min(2, max(1, int(proposed_capital / entry_price))) if entry_price > 0 else 0
    capital_used = shares * entry_price

    if shares == 0 or capital_used > wallet_balance:
        log_skipped_trade(symbol, "wallet_insufficient_funds", {
            "capital_required": capital_used,
            "available": wallet_balance
        })
        print(f"🚫 Not enough funds in wallet to trade {symbol}. Needed: ${capital_used:.2f}")
        return

    # 🧾 Try debit
    if not debit_wallet_balance(wallet_path, capital_used):
        print(f"❌ Wallet debit failed — aborting trade for {symbol}.")
        return
    else:
        print(f"💸 Debited ${capital_used:.2f} from wallet. New balance should reflect correctly.")

    vol_band_raw = signal.get("volatility_band", "low")
    if isinstance(vol_band_raw, (int, float)):
        if vol_band_raw >= 2.5:
            vol_band_str = "high"
        elif vol_band_raw >= 1.5:
            vol_band_str = "medium"
        else:
            vol_band_str = "low"
    else:
        vol_band_str = str(vol_band_raw)

    confidence_breakdown = signal.get("confidence_breakdown", {})
    strategies_used = signal.get("strategies_used", [])

    new_trade = {
        "symbol": symbol,
        "symbolId": symbol_id,
        "entry_time": now.isoformat(),
        "entry_price": round(entry_price, 2),
        "entry_price_source": quote_source,
        "exit_time": (now + pd.Timedelta(minutes=30)).isoformat(),
        "exit_price": None,
        "exit_price_source": None,
        "exit_reason": None,
        "pnl": None,
        "shares": shares,
        "capital_used": round(capital_used, 2),
        "entry_confidence": confidence,
        "confidence_breakdown": confidence_breakdown,
        "strategy": strategies_used[0] if strategies_used else "Unknown",
        "strategies_used": strategies_used,
        "volatility_band": vol_band_str,
        "win_rate": float(signal.get("win_rate", 0)),
        "context_features": {
            "MA20": context.get("MA20"),
            "volatility": context.get("volatility"),
            "rsi": context.get("rsi"),
            "bb_width": context.get("bb_width"),
            "macd_hist": context.get("macd_hist"),
            "time_of_day": now.strftime("%H:%M")
        }
    }

    print(f"📥 Logging trade: price={entry_price}, shares={shares}, capital={capital_used}")
    live_data = load_live_trades()
    live_data["trades"].append(new_trade)
    save_live_trades(live_data)
    print(f"✅ Trade for {symbol} appended successfully.")

