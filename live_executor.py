# live_executor.py – With Trade Skip Logging

import os
import json
import time
import pandas as pd
from datetime import datetime, time as dtime
import pytz
from dateutil.parser import isoparse
from modules.model_utils import add_technical_indicators
from modules.price_data import get_price_data
from modules.ai_signals_pro import get_strategy_signals
from modules.questrade import get_symbol_id, get_quote
from modules.Wallet_Utils import (
    debit_wallet_balance,
    get_wallet_available_capital,
    is_wallet_locked,
    lock_wallet
)
import holidays

# Configuration
MIN_CONFIDENCE = 0.75
TRADE_LOG_PATH = "exports/live_trades.json"
TRADE_QUEUE_PATH = "exports/trade_queue.json"
SKIP_LOG_PATH = "exports/skipped_trades.json"
WALLET_PATH = "exports/wallet.json"
SYMBOLS = ["MU", "AAPL", "NVDA", "TSLA"]
STARTING_CAPITAL = 10000
LOOP_INTERVAL = 30  # seconds

STRATEGY_WEIGHTS = {
    "Momentum": 1.2,
    "Breakout": 1.1,
    "VWAP_Bounce": 1.0,
    "MACD": 0.9,
}

if not is_wallet_locked():
    lock_wallet()

def get_market_now():
    return datetime.now(pytz.timezone("America/New_York"))

def is_market_open():
    now = get_market_now()
    us_holidays = holidays.US()
    market_open = dtime(9, 30)
    market_close = dtime(16, 0)
    print(f"🕒 Current time: {now}, Market open window: {market_open} to {market_close}")
    print(f"📅 Is holiday? {now.date() in us_holidays}")
    print(f"📆 Is weekday? {now.weekday() < 5}")
    return now.weekday() < 5 and now.date() not in us_holidays and market_open <= now.time() <= market_close

def weighted_confidence(signal):
    base = signal.get("confidence", 0.75)
    strategies = signal.get("strategies", [])
    if not strategies:
        return base
    weight = sum([STRATEGY_WEIGHTS.get(s, 1.0) for s in strategies]) / len(strategies)
    return base * weight

def log_skip(symbol, reason, context=None):
    log = {
        "symbol": symbol,
        "reason": reason,
        "time": get_market_now().isoformat(),
        "context": context or {}
    }
    existing = []
    if os.path.exists(SKIP_LOG_PATH):
        with open(SKIP_LOG_PATH, "r") as f:
            try:
                existing = json.load(f)
            except:
                existing = []
    existing.append(log)
    with open(SKIP_LOG_PATH, "w") as f:
        json.dump(existing[-200:], f, indent=2)

def load_symbols():
    return SYMBOLS

def load_trade_queue():
    try:
        if os.path.exists(TRADE_QUEUE_PATH):
            with open(TRADE_QUEUE_PATH, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to load queue, resetting: {e}")
    return []

def save_trade_queue(queue):
    def convert(obj):
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.isoformat()
        return obj
    with open(TRADE_QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2, default=convert)

def load_live_trades(path=TRADE_LOG_PATH):
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        fixed = {}
        for t in data:
            date = t.get("datetime", "")[:10]
            fixed.setdefault(date, {"trades": []})["trades"].append(t)
        return fixed
    return {}

def save_live_trades(trades):
    def convert(obj):
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        return obj
    with open(TRADE_LOG_PATH, "w") as f:
        json.dump(trades, f, indent=2, default=convert)

def fetch_live_signals(symbol):
    try:
        symbol_id = get_symbol_id(symbol)
        df = get_price_data(symbol_id, days=1, interval="OneMinute")
        if df is None or df.empty:
            print(f"⚠️ No data for {symbol}")
            return []

        df = add_technical_indicators(df)
        signals_df = get_strategy_signals(symbol, df, min_confidence=MIN_CONFIDENCE)
        if signals_df is None or signals_df.empty:
            return []

        valid_signals = signals_df[signals_df["signal"] == True].copy()
        if "datetime" not in valid_signals.columns:
            valid_signals.reset_index(inplace=True)
        return valid_signals.to_dict(orient="records")

    except Exception as e:
        print(f"❌ Error fetching signal for {symbol}: {e}")
        return []

def should_execute_trade(symbol, dt, existing_trades):
    return not any(t["symbol"] == symbol and t["status"] == "open" for t in existing_trades if isinstance(t, dict))

def simulate_trade_entry(symbol, signal, live_trades):
    now = get_market_now()
    if now.time() >= dtime(15, 15):
        log_skip(symbol, "late_entry", {"current_time": str(now.time())})
        return live_trades, False

    today = now.strftime("%Y-%m-%d")
    if today not in live_trades:
        live_trades[today] = {"trades": []}

    symbol_id = get_symbol_id(symbol)
    quote = get_quote(symbol_id)

    if quote and "lastTradePrice" in quote:
        entry_price = quote["lastTradePrice"]
        price_source = "quote"
    else:
        df = get_price_data(symbol_id, days=1, interval="OneMinute")
        entry_price = df["close"].iloc[-1] if not df.empty else None
        price_source = "fallback"

    if entry_price is None:
        log_skip(symbol, "no_entry_price")
        return live_trades, False

    if entry_price < 10:
        log_skip(symbol, "low_price", {"price": entry_price})
        return live_trades, False

    available = get_wallet_available_capital(WALLET_PATH)
    confidence = min(max(signal.get("confidence", 0.75), 0.1), 1.0)

    base_pct = 0.10
    bonus_pct = 0.20
    allocation_pct = base_pct + (bonus_pct * confidence)
    capital_to_use = min(available, STARTING_CAPITAL * allocation_pct)

    shares = int(capital_to_use // entry_price)
    if shares < 1:
        log_skip(symbol, "insufficient_capital", {"entry_price": entry_price, "capital": capital_to_use})
        return live_trades, False

    capital_used = round(shares * entry_price, 2)
    if not debit_wallet_balance(WALLET_PATH, capital_used):
        log_skip(symbol, "wallet_debit_failed", {"capital": capital_used})
        return live_trades, False

    trade = {
        "symbol": symbol,
        "symbolId": symbol_id,
        "datetime": now.isoformat(),
        "entry_price": entry_price,
        "entry_price_source": price_source,
        "confidence": signal["confidence"],
        "strategies_used": signal["strategies"],
        "status": "open",
        "shares": shares,
        "capital_used": capital_used
    }

    live_trades[today]["trades"].append(trade)
    print(f"🚀 BUY {shares}x {symbol} @ {entry_price:.2f} ({price_source}) [conf={signal['confidence']:.2f}]")
    return live_trades, True

def run_loop():
    print("🔁 Starting live_executor daemon loop...")
    while True:
        try:
            if not is_market_open():
                print("⏱️ Market is closed. Trading paused.")
                time.sleep(LOOP_INTERVAL)
                continue

            live_trades = load_live_trades()
            today = get_market_now().strftime("%Y-%m-%d")
            if today not in live_trades:
                live_trades[today] = {"trades": []}

            trade_queue = load_trade_queue()
            updated_queue = []

            for signal in trade_queue:
                symbol = signal["symbol"]
                dt = signal["datetime"]
                if should_execute_trade(symbol, dt, live_trades[today]["trades"]):
                    live_trades, executed = simulate_trade_entry(symbol, signal, live_trades)
                    if not executed:
                        updated_queue.append(signal)
                else:
                    log_skip(symbol, "already_traded")

            save_trade_queue(updated_queue[:10])

            all_signals = []
            for symbol in load_symbols():
                signals = fetch_live_signals(symbol)
                for signal in signals:
                    signal["symbol"] = symbol
                    signal["weighted_confidence"] = weighted_confidence(signal)
                    all_signals.append(signal)

            all_signals.sort(key=lambda s: s.get("weighted_confidence", 0), reverse=True)

            for signal in all_signals:
                symbol = signal["symbol"]
                dt = signal["datetime"]
                if should_execute_trade(symbol, dt, live_trades[today]["trades"]):
                    live_trades, executed = simulate_trade_entry(symbol, signal, live_trades)
                    if not executed:
                        updated_queue.append(signal)
                else:
                    log_skip(symbol, "already_traded")

            save_live_trades(live_trades)
            save_trade_queue(updated_queue[:0])

            print(f"📊 Queued trades remaining: {len(updated_queue)}")
            print(f"✅ Trade scan complete. Waiting {LOOP_INTERVAL}s...\n")
            time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            print("🛑 Stopped live_executor loop.")
            break
        except Exception as e:
            print(f"❌ Unexpected error in loop: {e}")
            time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()
