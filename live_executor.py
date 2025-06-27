#live_executor_atomic.py

import os
import json
import time
import pandas as pd
from datetime import datetime, time as dtime
import pytz
from dateutil.parser import isoparse
import holidays
from utils import atomic_save_json, update_system_status
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

def load_strategy_config(path):
    with open(path) as f:
        return json.load(f)

CONFIG = load_strategy_config("inputs/ai_strategy_configs/live_params.json")
PENALTY_TRACKER_PATH = "exports/penalty_tracker.json"
SYMBOL_ID_CACHE_FILE = "exports/symbol_id_cache.json"

# Configuration
MIN_CONFIDENCE = 0.75
TRADE_LOG_PATH = "exports/live_trades.json"
TRADE_QUEUE_PATH = "exports/trade_queue.json"
SKIP_LOG_PATH = "exports/skipped_trades.json"
WALLET_PATH = "exports/wallet.json"
STARTING_CAPITAL = 10000
LOOP_INTERVAL = 30  # seconds

STRATEGY_WEIGHTS = CONFIG.get("strategy_weights", {
    "Momentum": 1.0,
    "Breakout": 1.0,
    "VWAP_Bounce": 1.0,
    "MACD": 1.0,
})

if not is_wallet_locked():
    lock_wallet()

# Initialize cache if not present
if os.path.exists(SYMBOL_ID_CACHE_FILE):
    with open(SYMBOL_ID_CACHE_FILE, "r") as f:
        SYMBOL_ID_CACHE = json.load(f)
else:
    SYMBOL_ID_CACHE = {}

def get_symbol_id_with_cache(symbol):
    if symbol in SYMBOL_ID_CACHE:
        return SYMBOL_ID_CACHE[symbol]
    symbol_id = get_symbol_id(symbol)  # returns int directly
    SYMBOL_ID_CACHE[symbol] = symbol_id
    with open(SYMBOL_ID_CACHE_FILE, "w") as f:
        json.dump(SYMBOL_ID_CACHE, f, indent=2)
    return symbol_id

quote_cache = {}

def get_cached_quote(symbol):
    symbol_id = get_symbol_id_with_cache(symbol)  # always ensure correct int symbol_id
    if symbol_id in quote_cache:
        return quote_cache[symbol_id]
    quote = get_quote(symbol_id)
    quote_cache[symbol_id] = quote
    return quote

quote_cache = {}

# Then, modify get_cached_quote to refresh per loop while caching within that loop:
def get_cached_quote(symbol):
    symbol_id = get_symbol_id_with_cache(symbol)
    # Always fetch live quotes on each call without cross-loop caching:
    quote = get_quote(symbol_id)
    return quote

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

def load_penalty_tracker(path=PENALTY_TRACKER_PATH):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_penalty_tracker(data, path=PENALTY_TRACKER_PATH):
    atomic_save_json(data, path)

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
    atomic_save_json(queue, TRADE_QUEUE_PATH)

def load_frequent_signals(path="exports/frequent_signals.json"):
    if not os.path.exists(path):
        print("⚠️ frequent_signals.json not found.")
        return []
    try:
        with open(path, "r") as f:
            signals = json.load(f)
        for s in signals:
            s["datetime"] = s.get("timestamp", s.get("datetime"))
            s["weighted_confidence"] = weighted_confidence(s)
        return signals
    except Exception as e:
        print(f"⚠️ Failed to load frequent signals: {e}")
        return []

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

def save_live_trades(trades, path=TRADE_LOG_PATH):
    def convert(obj):
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        return obj
    #print("⚠️ Save live trades.")
    atomic_save_json(trades, path)

def load_signals_from_json(path="exports/live_signals.json"):
    if not os.path.exists(path):
        print("⚠️ live_signals.json not found.")
        return []
    try:
        with open(path, "r") as f:
            signals = json.load(f)
        for s in signals:
            s["datetime"] = s.get("timestamp", s.get("datetime"))
            s["weighted_confidence"] = weighted_confidence(s)
        return signals
    except Exception as e:
        print(f"⚠️ Failed to load signals from JSON: {e}")
        return []

def should_execute_trade(symbol, dt, existing_trades):
    return not any(t["symbol"] == symbol and t["status"] == "open" for t in existing_trades if isinstance(t, dict))


def get_recent_performance(symbol, trades, window=20):
    recent = []
    for day_data in trades.values():
        for t in day_data.get("trades", []):
            if t["symbol"] == symbol and "exit_price" in t:
                entry = t["entry_price"]
                exit_ = t["exit_price"]
                pnl_pct = (exit_ - entry) / entry
                recent.append(pnl_pct)
    if not recent:
        return 0.0, 0.0
    recent = recent[-window:]
    avg_pnl = sum(recent) / len(recent)
    max_dd = min(recent)
    return avg_pnl, max_dd

def score_symbol_quality(symbol, trades, quote, config):
    avg_pnl, max_dd = get_recent_performance(symbol, trades, config["lookback_trades"])
    score = 0
    if avg_pnl > config["min_avg_pnl_boost"]: score += 1
    if avg_pnl < config["min_avg_pnl_penalty"]: score -= 1
    if max_dd < config["max_drawdown_penalty"]: score -= 1
    if quote:
        spread = abs(quote.get("askPrice", 0) - quote.get("bidPrice", 0))
        price = quote.get("lastTradePrice", 1)
        spread_pct = spread / price if price > 0 else 0
        if spread_pct > config["max_spread"]: score -= 1
        if quote.get("volume", 0) > config["min_volume"]: score += 1
    return score


def simulate_trade_entry(symbol, signal, live_trades):
    now = get_market_now()
    if now.time() >= dtime(15, 15):
        log_skip(symbol, "late_entry", {"current_time": str(now.time())})
        return live_trades, False

    today = now.strftime("%Y-%m-%d")
    if today not in live_trades:
        live_trades[today] = {"trades": []}

    symbol_id = get_symbol_id_with_cache(symbol)
    quote = get_cached_quote(symbol)

    entry_price = quote.get("lastTradePrice")
    price_source = "quote" if entry_price else "fallback"

    if not entry_price:
        df = get_price_data(symbol_id, days=1, interval="OneMinute")
        entry_price = df["close"].iloc[-1] if not df.empty else None

    if entry_price is None:
        log_skip(symbol, "no_entry_price")
        return live_trades, False

    if entry_price < 10:
        log_skip(symbol, "low_price", {"price": entry_price})
        return live_trades, False

    available = get_wallet_available_capital(WALLET_PATH)
    confidence = min(max(signal.get("confidence", 0.75), 0.1), 1.0)

    allocation_config = CONFIG.get("capital_allocation", {})
    base_pct = allocation_config.get("base_pct", 0.10)
    bonus_pct = allocation_config.get("bonus_pct", 0.20)
    max_pct = allocation_config.get("max_allocation_pct", 0.40)

    allocation_pct = min(base_pct + (bonus_pct * confidence), max_pct)
    capital_to_use = min(available, STARTING_CAPITAL * allocation_pct)

    shares = int(capital_to_use // entry_price)
    if shares < 1:
        log_skip(symbol, "insufficient_capital", {"entry_price": entry_price, "capital": capital_to_use})
        return live_trades, False

    capital_used = round(shares * entry_price, 2)
    if not debit_wallet_balance(WALLET_PATH, capital_used):
        log_skip(symbol, "wallet_debit_failed", {"capital": capital_used})
        return live_trades, False

    spread = abs(quote.get("askPrice", 0) - quote.get("bidPrice", 0))
    spread_pct = spread / entry_price if entry_price else 0

    trade = {
        "symbol": symbol,
        "symbolId": symbol_id,
        "datetime": now.isoformat(),
        "entry_price": entry_price,
        "entry_price_source": price_source,
        "confidence": signal["confidence"],
        "strategies_used": signal.get("strategies", []),
        "spread_pct": round(spread_pct, 4),
        "quote_snapshot": {
            "bid": quote.get("bidPrice"),
            "ask": quote.get("askPrice"),
            "volume": quote.get("volume")
        },
        "status": "open",
        "shares": shares,
        "capital_used": capital_used,
        "allocation_log": {
            "confidence": signal["confidence"],
            "allocation_pct": round(allocation_pct, 4),
            "capital_allocated": capital_used,
            "wallet_available_before": available,
            "wallet_debited": capital_used,
            "price_source": price_source
        }
    }

    live_trades[today]["trades"].append(trade)
    print(f"🚀 BUY {shares}x {symbol} @ {entry_price:.2f} ({price_source}) [conf={signal['confidence']:.2f}]")
    return live_trades, True

def run_loop():
    print("🔁 Starting live_executor daemon loop with quote caching...")
    while True:
        loop_start = time.time()
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

            for signal in sorted(load_frequent_signals() + load_signals_from_json(), key=lambda s: s.get("weighted_confidence", 0), reverse=True):
                symbol = signal["symbol"]
                dt = signal["datetime"]
                quote = get_cached_quote(symbol)
                score = score_symbol_quality(symbol, live_trades, quote, CONFIG.get("performance_filter", {}))
                if score < 0:
                    log_skip(symbol, "symbol_quality_score", {"score": score})
                    continue
                if should_execute_trade(symbol, dt, live_trades[today]["trades"]):
                    live_trades, executed = simulate_trade_entry(symbol, signal, live_trades)
                    if not executed:
                        updated_queue.append(signal)
                else:
                    log_skip(symbol, "already_traded")

            save_live_trades(live_trades)
            update_system_status("live_executor", {
                "last_executed": datetime.now().isoformat(),
                "open_trades": len([t for t in live_trades[today]["trades"] if t["status"] == "open"])
            })
            print(f"📊 Queued trades remaining: {len(updated_queue)}")

        except KeyboardInterrupt:
            print("🛑 Stopped live_executor loop.")
            break
        except Exception as e:
            print(f"❌ Unexpected error in loop: {e}")

        elapsed = time.time() - loop_start
        sleep_time = max(0, LOOP_INTERVAL - elapsed)
        print(f"⏱️ Loop completed in {elapsed:.2f}s, sleeping {sleep_time:.2f}s to maintain {LOOP_INTERVAL}s cycle.")
        time.sleep(sleep_time)
if __name__ == "__main__":
    run_loop()
