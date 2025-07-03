# backtest_engine.py - Final Stable Live-Simulation with Cooldown, Cap, SELL logic, Immediate JSON Writes

import os
import json
import time
from datetime import datetime, timedelta, time as dtime
from pytz import timezone
import holidays
import pandas as pd
from modules.price_data import get_price_data
from modules.ai_signals_pro import get_strategy_signals
from modules.model_utils import extract_features, load_model, add_technical_indicators
from modules.questrade import get_symbol_id
from utils import atomic_save_json, update_system_status

# === CONFIG ===
LOCAL_TZ = timezone("America/Toronto")
us_holidays = holidays.US()
SYMBOLS = ["SYM", "FSLR", "DDOG" ,"GITS","RGC"]
ORDERS_PATH = "exports/backtest_orders.json"
CONTROL_PATH = "inputs/ai_strategy_configs/control_params.json"
LIVE_TRADES_PATH = "exports/live_trades.json"
RECOVERY_TRACK_FILE = "exports/recovery_tracker.json"
WALLET_PATH = "exports/wallet.json"
LAST_EXIT_TRACKER_PATH = "exports/last_exit_tracker.json"
MODEL = load_model()
LOOP_INTERVAL = 30
MIN_CONFIDENCE = 0.5
TOKEN_PATH = "C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json"
RECOVERY_SIMULATIONS_REQUIRED = 3
CAPITAL_LIMIT = 10000
MIN_REBUY_DELAY = 300  # 5 min

# === UTILITIES ===
def market_is_open():
    now = datetime.now(LOCAL_TZ)
    if now.date() in us_holidays:
        return False
    return now.weekday() < 5 and dtime(9, 30) <= now.time() <= dtime(16, 0)

def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_json(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_wallet_balance():
    wallet = load_json(WALLET_PATH)
    return wallet.get("balance", 0)

def simulate_sandbox(symbol, passes=2):
    success_count = 0
    for _ in range(passes):
        df = get_price_data(get_symbol_id(symbol, TOKEN_PATH), days=2, interval="OneMinute")
        if df is None or df.empty or "close" not in df.columns:
            continue

        closes = df["close"]
        if len(closes) < 20:
            continue

        # Basic PnL check
        pnl_pct = (closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0] * 100

        # Uptrend check: last close > last 5-period mean
        last_close = closes.iloc[-1]
        ma5 = closes.tail(5).mean()

        # Volatility check: std deviation of last 10 closes
        volatility = closes.tail(10).std() / closes.tail(10).mean()

        # Pass criteria
        if pnl_pct > 0.1 and last_close > ma5 and volatility < 0.02:
            success_count += 1

        time.sleep(0.5)

    return success_count >= (passes // 2 + 1)


def calculate_allocation_shares(symbol, price, confidence, available_capital):
    if price is None or available_capital <= 0:
        return 0, 0
    max_capital = min(available_capital, CAPITAL_LIMIT)
    if confidence < 0.7:
        max_capital = min(max_capital, price * 10)
    shares = max(int(max_capital // price), 0)
    capital_allocated = round(shares * price, 2)
    return shares, capital_allocated

# === MAIN LOOP ===
def generate_orders():
    if not market_is_open():
        print(f"[{datetime.now(LOCAL_TZ)}] 🛑 Market closed.")
        return

    control = load_json(CONTROL_PATH)
    suspended = control.get("suspended_symbols", [])
    prioritized = control.get("prioritized_symbols", [])
    live_trades = load_json(LIVE_TRADES_PATH)
    recovery = load_json(RECOVERY_TRACK_FILE)
    last_exit_tracker = load_json(LAST_EXIT_TRACKER_PATH)
    pending_orders = load_json(ORDERS_PATH)
    pending_symbols = {o["symbol"] for o in pending_orders if o["action"] == "BUY"}

    wallet_balance = get_wallet_balance()
    deployed = sum(
        trade.get("capital_used", 0)
        for day in live_trades.values()
        for trade in day.get("trades", [])
        if trade.get("status") == "open"
    )
    available_capital = max(0, min(wallet_balance, CAPITAL_LIMIT - deployed))
    total_pending_allocation = 0

    orders = []

    for symbol in SYMBOLS:
        if symbol in pending_symbols:
            print(f"⏳ Skipping {symbol}, pending BUY in orders.json.")
            continue

        # Cooldown after negative exit
        last_exit_record = last_exit_tracker.get(symbol, {})
        last_exit_time = last_exit_record.get("time")
        last_exit_pnl = last_exit_record.get("pnl", 0)

        if last_exit_time:
            last_exit_dt = datetime.fromisoformat(last_exit_time)
            cooldown_elapsed = (datetime.now(LOCAL_TZ) - last_exit_dt).total_seconds()
            if last_exit_pnl < 0 and cooldown_elapsed < MIN_REBUY_DELAY:
                print(f"⏳ Cooldown: Skipping {symbol} after recent negative exit (PnL {last_exit_pnl}).")
                continue
            elif last_exit_pnl >= 0:
                if not simulate_sandbox(symbol, passes=2):
                    print(f"🚫 Sandbox fail post-positive exit: {symbol}.")
                    continue

        if symbol in suspended:
            if simulate_sandbox(symbol):
                recovery[symbol] = recovery.get(symbol, 0) + 1
                print(f"🩺 Sandbox recovery {symbol}: {recovery[symbol]}/{RECOVERY_SIMULATIONS_REQUIRED}")
                if recovery[symbol] >= RECOVERY_SIMULATIONS_REQUIRED:
                    suspended.remove(symbol)
                    recovery.pop(symbol, None)
                    print(f"✅ {symbol} reactivated.")
            else:
                recovery[symbol] = 0
            continue

        in_trade = any(
            trade.get("status") == "open" and trade.get("symbol") == symbol
            for day in live_trades.values()
            for trade in day.get("trades", [])
        )
        if in_trade:
            df = get_price_data(get_symbol_id(symbol, TOKEN_PATH), days=2, interval="OneMinute")
            if df is None or df.empty:
                continue
            latest = add_technical_indicators(df).iloc[-1]
            price = latest.get("close")
            exit_reasons = []
            if price < latest.get("vwap", price): exit_reasons.append("vwap_reversion")
            if latest.get("rsi", 50) > 70: exit_reasons.append("rsi_overbought")
            if latest.get("macd_hist", 0) < 0: exit_reasons.append("macd_bearish")
            if price < latest.get("ema20", price): exit_reasons.append("ma_breakdown")
            if exit_reasons:
                entry_price, shares = None, None
                for day in live_trades.values():
                    for trade in day.get("trades", []):
                        if trade.get("status") == "open" and trade.get("symbol") == symbol:
                            entry_price = trade.get("price")
                            shares = trade.get("shares")
                            break
                    if entry_price and shares:
                        break
                pnl_value = (price - entry_price) * shares if entry_price and shares else 0
                sell_order = {
                    "symbol": symbol,
                    "action": "SELL",
                    "price": price,
                    "pnl": round(pnl_value, 2),
                    "timestamp": datetime.now(LOCAL_TZ).isoformat(),
                    "exit_reasons": exit_reasons,
                    "allocation_notes": "Auto exit"
                }
                orders.append(sell_order)
                atomic_save_json(orders, ORDERS_PATH)
                last_exit_tracker[symbol] = {
                    "time": datetime.now(LOCAL_TZ).isoformat(),
                    "pnl": round(pnl_value, 2)
                }
                print(f"✅ SELL Order: {symbol} @ {price} | {exit_reasons}")
            continue

        if not simulate_sandbox(symbol):
            print(f"🚫 Skipping {symbol}, sandbox pre-buy check failed.")
            continue

        df = get_price_data(get_symbol_id(symbol, TOKEN_PATH), days=2, interval="OneMinute")
        if df is None or df.empty:
            continue
        signals_df = get_strategy_signals(symbol, df, min_confidence=0.0)
        if signals_df.empty:
            continue
        features_df = extract_features(df)
        if MODEL:
            X = features_df[MODEL.feature_names_in_].fillna(0)
            preds = MODEL.predict_proba(X)[:, 1]
            signals_df["ml_score"] = preds[-len(signals_df):]
            signals_df["hybrid_confidence"] = 0.6 * signals_df["confidence"] + 0.4 * signals_df["ml_score"]
        else:
            signals_df["hybrid_confidence"] = signals_df["confidence"]

        best = signals_df.iloc[signals_df["hybrid_confidence"].idxmax()]
        confidence = best["hybrid_confidence"]
        if confidence < MIN_CONFIDENCE:
            continue

        price = best.get("close") or df["close"].iloc[-1]
        shares, capital_allocated = calculate_allocation_shares(symbol, price, confidence, available_capital - total_pending_allocation)
        if capital_allocated <= 0 or (total_pending_allocation + capital_allocated) > CAPITAL_LIMIT:
            print(f"⚠️ Skipping {symbol}, insufficient capital or cap exceeded.")
            continue

        total_pending_allocation += capital_allocated

        order = {
            "symbol": symbol,
            "action": "BUY",
            "confidence": round(confidence, 3),
            "price": price,
            "shares": shares,
            "capital_allocated": capital_allocated,
            "timestamp": datetime.now(LOCAL_TZ).isoformat(),
            "allocation_notes": "Strong" if confidence >= 0.7 else "Weak"
        }
        orders.append(order)
        atomic_save_json(orders, ORDERS_PATH)
        print(f"✅ BUY Order: {symbol} @ {price} | {shares} shares | {order['allocation_notes']}")

    save_json(control, CONTROL_PATH)
    save_json(recovery, RECOVERY_TRACK_FILE)
    save_json(last_exit_tracker, LAST_EXIT_TRACKER_PATH)
    update_system_status("backtest_engine", {"orders_generated": len(orders), "timestamp": datetime.now(LOCAL_TZ).isoformat()})

# === LOOP ===
def run_loop():
    while True:
        generate_orders()
        print(f"[{datetime.now(LOCAL_TZ)}] ⏱️ Sleeping {LOOP_INTERVAL}s before next cycle.")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()
