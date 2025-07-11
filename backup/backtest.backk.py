# backtest_engine.py - Final clean version with momentum priority and your full exit logic

import os
import json
import time
from datetime import datetime, timedelta, time as dtime
from pytz import timezone as pytz_timezone
import holidays
import pandas as pd

from modules.price_data import get_price_data
from modules.ai_signals_pro import get_strategy_signals
from modules.model_utils import extract_features, load_model, add_technical_indicators
from utils import atomic_save_json, update_system_status
from modules.log_utils import log, fail_safe
from polygon_utils import load_gainers_symbols, ensure_gainers_symbols, get_cached_5d_high

LOCAL_TZ = pytz_timezone("America/Toronto")
us_holidays = holidays.US()
ORDERS_PATH = "exports/backtest_orders.json"
CONTROL_PATH = "inputs/ai_strategy_configs/control_params.json"
LIVE_TRADES_PATH = "exports/live_trades.json"
RECOVERY_TRACK_FILE = "exports/recovery_tracker.json"
WALLET_PATH = "exports/wallet.json"
LAST_EXIT_TRACKER_PATH = "exports/last_exit_tracker.json"
MODEL = load_model()
LOOP_INTERVAL = 30
MIN_CONFIDENCE = 0.5
RECOVERY_SIMULATIONS_REQUIRED = 3
CAPITAL_LIMIT = 10000
MIN_REBUY_DELAY = 300  # 5 min
MAX_NON_MOMENTUM_TRADES = 4

# === UTILITIES ===
def market_is_open():
    now = datetime.now(LOCAL_TZ)
    if now.date() in us_holidays:
        return False
    return now.weekday() < 5 and dtime(9, 30) <= now.time() <= dtime(16, 0)

def load_json(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        log(f"Error loading JSON from {path}: {e}", "ERROR", "IO")
    return {}

def save_json(data, path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Error saving JSON to {path}: {e}", "ERROR", "IO")
        
def get_wallet_balance():
    wallet = load_json(WALLET_PATH)
    return wallet.get("balance", 0)



def simulate_sandbox(symbol, passes=2):
    success_count = 0
    for i in range(passes):
        df = get_price_data(symbol, days=2, interval="minute")
        if df is None or df.empty or "close" not in df.columns:
            continue
        closes = df["close"]
        if len(closes) < 20:
            continue
        entry_price = closes.iloc[0]
        exit_price = closes.iloc[-1]
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        if pnl_pct > 0:
            success_count += 1
        time.sleep(0.2)
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

# === MAIN TRADING LOOP ===
@fail_safe(module="BacktestEngine", critical=True)
def generate_orders():
    if not market_is_open():
        log("Market closed.", "INFO", "BacktestEngine")
        return

    now = datetime.now(LOCAL_TZ)
    if now.time() >= dtime(16, 0):
        log("🛑 Market closed at 16:00, skipping order generation.", "INFO", "BacktestEngine")
        return

    control = load_json(CONTROL_PATH)
    suspended = control.get("suspended_symbols", [])
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
    sold_symbols = set()

    momentum_symbols, non_momentum_symbols = load_gainers_symbols()

    if not momentum_symbols and not non_momentum_symbols:
        log("❌ No symbols available to trade from top_gainers.json. Check upstream pipeline.", "ERROR", "BacktestEngine")
        return

    active_non_momentum_trades = [
        trade.get("symbol") for day in live_trades.values()
        for trade in day.get("trades", [])
        if trade.get("status") == "open" and trade.get("symbol") not in momentum_symbols
    ]
    slots_available = MAX_NON_MOMENTUM_TRADES - len(active_non_momentum_trades)

    # Priority logic:
    # Always prioritize momentum
    if momentum_symbols:
        symbols_to_trade = momentum_symbols.copy()
        log(f"✅ Using {len(symbols_to_trade)} momentum symbols for trading.", "INFO", "BacktestEngine")
    else:
        # If no momentum, use as many non-momentum as slots allow
        if slots_available > 0:
            symbols_to_trade = non_momentum_symbols[:slots_available]
            log(f"✅ Using {len(symbols_to_trade)} non-momentum symbols (slots available: {slots_available}).", "INFO", "BacktestEngine")
        else:
            symbols_to_trade = []
            log(f"⚠️ No slots available for non-momentum symbols this cycle (slots exhausted).", "WARNING", "BacktestEngine")

    log("✅ Starting cycle: Checking open trades and generating new orders.", "INFO", "BacktestEngine")

    for symbol in symbols_to_trade:
        is_momentum = symbol in momentum_symbols

        if symbol in pending_symbols:
            log(f"⏳ Skipping {symbol}, pending BUY detected.", "INFO", "BacktestEngine")
            continue

        # SELL PROCESSING FOR OPEN TRADES
        open_trades_for_symbol = [
            trade for day in live_trades.values()
            for trade in day.get("trades", [])
            if trade.get("status") == "open" and trade.get("symbol") == symbol
        ]

        if open_trades_for_symbol:
            log(f"🔍 Found {len(open_trades_for_symbol)} open trade(s) for {symbol}. Checking SELL conditions...", "DEBUG", "BacktestEngine")

        for day in live_trades.values():
            for trade in day.get("trades", []):
                if trade.get("status") != "open":
                    continue
                entry_price = trade.get("entry_price")
                shares = trade.get("shares")
                trade_entry_time_str = trade.get("datetime")
                if not entry_price or not shares or not trade_entry_time_str:
                    log(f"⚠️ Incomplete trade data for {symbol}, skipping.", "WARNING", "BacktestEngine")
                    continue

                df = get_price_data(symbol, days=2, interval="minute")
                if df is None or df.empty:
                    log(f"⚠️ No price data for {symbol}, skipping SELL check.", "WARNING", "BacktestEngine")
                    continue

                closes = df["close"]
                price = closes.iloc[-1]
                pnl_value = (price - entry_price) * shares
                pnl_pct = ((price - entry_price) / entry_price) * 100

                trade_entry_time = datetime.fromisoformat(trade_entry_time_str)
                elapsed_secs = (datetime.now(LOCAL_TZ) - trade_entry_time).total_seconds()
                log(f"⏱️ {symbol} held for {elapsed_secs/60:.2f} min | PnL: {pnl_pct:.2f}%", "DEBUG", "BacktestEngine")

                exit_reasons = []

                # Forced exit if held > 30 min with small PnL
                if elapsed_secs > 1800 and pnl_pct >= 0.5:
                    exit_reasons.append("time_exit_after_30min_small_pnl")
                    log(f"⏰ Exiting {symbol} after {elapsed_secs/60:.1f} min with PnL {pnl_pct:.2f}%", "INFO", "BacktestEngine")

                if exit_reasons:
                    sell_order = {
                        "symbol": symbol,
                        "action": "SELL",
                        "price": price,
                        "pnl": round(pnl_value, 2),
                        "timestamp": datetime.now(LOCAL_TZ).isoformat(),
                        "exit_reasons": exit_reasons,
                        "allocation_notes": "Auto Exit"
                    }
                    orders.append(sell_order)
                    atomic_save_json(orders, ORDERS_PATH)
                    last_exit_tracker[symbol] = {
                        "time": datetime.now(LOCAL_TZ).isoformat(),
                        "pnl": round(pnl_value, 2)
                    }
                    sold_symbols.add(symbol)
                    log(f"✅ SELL {symbol} @ {price:.2f} | PnL: {round(pnl_value, 2)} | Reasons: {exit_reasons}", "INFO", "BacktestEngine")
                else:
                    log(f"🔄 Holding {symbol}, conditions for SELL not met.", "DEBUG", "BacktestEngine")
                continue

        now = datetime.now(LOCAL_TZ)
        if now.time() >= dtime(15, 30):
            allow_new_buys = False
        else:
            allow_new_buys = True
        if not allow_new_buys:
            log(f"🛑 Skipping BUY for {symbol} after 15:30 cutoff.", "INFO", "BacktestEngine")
            continue  
        if not simulate_sandbox(symbol):
            log(f"🚫 Skipping {symbol}, sandbox pre-buy check failed.", "WARNING", "BacktestEngine")
            continue
        
        #high_5d = get_cached_5d_high(symbol)
        #if high_5d is None:
            log(f"⚠️ No 5d high found for {symbol}, skipping BUY.", "WARNING", "BacktestEngine")
            continue

        #limit_pct = 1 if is_momentum else 1
        #price_limit = high_5d * limit_pct

        # get current price for comparison
        #df_check_price = get_price_data(symbol, days=1, interval="minute")
        #if df_check_price is None or df_check_price.empty:
            log(f"⚠️ No price data for {symbol} for 5d check, skipping.", "WARNING", "BacktestEngine")
            continue

        #current_price = df_check_price["close"].iloc[-1]

        #if current_price > price_limit:
            log(f"🚫 Skipping {symbol} BUY: current price {current_price:.2f} exceeds {limit_pct*100:.0f}% of 5d high ({high_5d:.2f}).", "INFO", "BacktestEngine")
            continue

        
             
        df = get_price_data(symbol, days=2, interval="minute")
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
        effective_confidence = 0.3 if is_momentum else MIN_CONFIDENCE
        if confidence < effective_confidence:
            continue

        price = best.get("close") or df["close"].iloc[-1]

        if is_momentum:
            max_capital = min(CAPITAL_LIMIT * 0.5, available_capital - total_pending_allocation)
            shares = max(int(max_capital // price), 0)
            capital_allocated = round(shares * price, 2)
        else:
            shares, capital_allocated = calculate_allocation_shares(symbol, price, confidence, available_capital - total_pending_allocation)

        if capital_allocated <= 0 or (total_pending_allocation + capital_allocated) > CAPITAL_LIMIT:
            log(f"⚠️ Skipping {symbol}, insufficient capital or cap exceeded.", "WARNING", "BacktestEngine")
            continue

        total_pending_allocation += capital_allocated
        if not is_momentum:
            sandbox_passed = simulate_sandbox(symbol)
            if not sandbox_passed:
                log(f"🚫 Skipping {symbol}, sandbox pre-check failed (non-momentum).", "WARNING", "BacktestEngine")
                continue
            
        order = {
            "symbol": symbol,
            "action": "BUY",
            "confidence": round(confidence, 3),
            "price": price,  # keep for reference
            "entry_price_triggered": price,  # NEW: explicitly save triggered price
            "shares": shares,
            "capital_allocated": capital_allocated,
            "timestamp": datetime.now(LOCAL_TZ).isoformat(),
            "allocation_notes": "Momentum" if is_momentum else ("Strong" if confidence >= 0.7 else "Weak")
        }
        orders.append(order)
        atomic_save_json(orders, ORDERS_PATH)
        log(f"{'📈 Momentum BUY' if is_momentum else '✅ BUY'} {symbol} @ {price} | {shares} shares | {order['allocation_notes']}", "INFO", "BacktestEngine")
        print(f"{'📈 Momentum BUY' if is_momentum else '✅ BUY'} {symbol} @ {price} | {shares} shares | {order['allocation_notes']}", "INFO", "BacktestEngine")
    save_json(control, CONTROL_PATH)
    save_json(recovery, RECOVERY_TRACK_FILE)
    save_json(last_exit_tracker, LAST_EXIT_TRACKER_PATH)
    update_system_status("backtest_engine", {"orders_generated": len(orders), "timestamp": datetime.now(LOCAL_TZ).isoformat()})
    log(f"Completed cycle with {len(orders)} orders.", "INFO", "BacktestEngine")

# === LOOP ===
def run_loop():
    while True:
        generate_orders()
        log(f"Sleeping {LOOP_INTERVAL}s before next cycle.", "INFO", "BacktestEngine")
        time.sleep(LOOP_INTERVAL)
# === LOOP ===
def run_loop():
    while True:
        generate_orders()
        log(f"Sleeping {LOOP_INTERVAL}s before next cycle.", "INFO", "BacktestEngine")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()


@fail_safe(module="BacktestEngine", critical=True)
def generate_orders():
    if not market_is_open():
        log("Market closed.", "INFO", "BacktestEngine")
        return

    now = datetime.now(LOCAL_TZ)
    if now.time() >= dtime(16, 0):
        log("🛑 Market closed at 16:00, skipping order generation.", "INFO", "BacktestEngine")
        return

    control = load_json(CONTROL_PATH)
    suspended = control.get("suspended_symbols", [])
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
    sold_symbols = set()

    # === SELL PROCESSING FOR ALL OPEN TRADES ===
    log("✅ Starting SELL checks on all open trades.", "INFO", "BacktestEngine")
    for day in live_trades.values():
        for trade in day.get("trades", []):
            if trade.get("status") != "open":
                continue
            symbol = trade.get("symbol")
            entry_price = trade.get("entry_price")
            shares = trade.get("shares")
            trade_entry_time_str = trade.get("datetime")

            if not entry_price or not shares or not trade_entry_time_str:
                log(f"⚠️ Incomplete trade data for {symbol}, skipping.", "WARNING", "BacktestEngine")
                continue

            df = get_price_data(symbol, days=2, interval="minute")
            if df is None or df.empty:
                log(f"⚠️ No price data for {symbol}, skipping SELL check.", "WARNING", "BacktestEngine")
                continue

            closes = df["close"]
            price = closes.iloc[-1]
            pnl_value = (price - entry_price) * shares
            pnl_pct = ((price - entry_price) / entry_price) * 100

            trade_entry_time = datetime.fromisoformat(trade_entry_time_str)
            elapsed_secs = (datetime.now(LOCAL_TZ) - trade_entry_time).total_seconds()
            log(f"⏱️ {symbol} held for {elapsed_secs/60:.2f} min | PnL: {pnl_pct:.2f}%", "DEBUG", "BacktestEngine")

            exit_reasons = []

            # Forced exit if held > 30 min with PnL >= 0.5%
            if elapsed_secs > 1800 and pnl_pct >= 0.5:
                exit_reasons.append("time_exit_after_30min_small_pnl")
                log(f"⏰ Exiting {symbol} after {elapsed_secs/60:.1f} min with PnL {pnl_pct:.2f}%", "INFO", "BacktestEngine")

            if exit_reasons:
                sell_order = {
                    "symbol": symbol,
                    "action": "SELL",
                    "price": price,
                    "pnl": round(pnl_value, 2),
                    "timestamp": datetime.now(LOCAL_TZ).isoformat(),
                    "exit_reasons": exit_reasons,
                    "allocation_notes": "Auto Exit"
                }
                orders.append(sell_order)
                atomic_save_json(orders, ORDERS_PATH)
                last_exit_tracker[symbol] = {
                    "time": datetime.now(LOCAL_TZ).isoformat(),
                    "pnl": round(pnl_value, 2)
                }
                sold_symbols.add(symbol)
                log(f"✅ SELL {symbol} @ {price:.2f} | PnL: {round(pnl_value, 2)} | Reasons: {exit_reasons}", "INFO", "BacktestEngine")
            else:
                log(f"🔄 Holding {symbol}, conditions for SELL not met.", "DEBUG", "BacktestEngine")

    # === BUY PROCESSING ===
    momentum_symbols, non_momentum_symbols = load_gainers_symbols()
    if not momentum_symbols and not non_momentum_symbols:
        log("❌ No symbols available to trade from top_gainers.json. Check upstream pipeline.", "ERROR", "BacktestEngine")
        return

    active_non_momentum_trades = [
        trade.get("symbol") for day in live_trades.values()
        for trade in day.get("trades", [])
        if trade.get("status") == "open" and trade.get("symbol") not in momentum_symbols
    ]
    slots_available = MAX_NON_MOMENTUM_TRADES - len(active_non_momentum_trades)

    if momentum_symbols:
        symbols_to_trade = momentum_symbols.copy()
        log(f"✅ Using {len(symbols_to_trade)} momentum symbols for trading.", "INFO", "BacktestEngine")
    else:
        if slots_available > 0:
            symbols_to_trade = non_momentum_symbols[:slots_available]
            log(f"✅ Using {len(symbols_to_trade)} non-momentum symbols (slots available: {slots_available}).", "INFO", "BacktestEngine")
        else:
            log(f"⚠️ No slots available for non-momentum symbols this cycle (slots exhausted).", "WARNING", "BacktestEngine")
            return

    log("✅ Starting BUY checks and order generation.", "INFO", "BacktestEngine")

    for symbol in symbols_to_trade:
        is_momentum = symbol in momentum_symbols

        now = datetime.now(LOCAL_TZ)
        if now.time() >= dtime(15, 30):
            allow_new_buys = False
        else:
            allow_new_buys = True
        if not allow_new_buys:
            log(f"🛑 Skipping BUY for {symbol} after 15:30 cutoff.", "INFO", "BacktestEngine")
            continue  
        if not simulate_sandbox(symbol):
            log(f"🚫 Skipping {symbol}, sandbox pre-buy check failed.", "WARNING", "BacktestEngine")
            continue
        
        #high_5d = get_cached_5d_high(symbol)
        #if high_5d is None:
        #    log(f"⚠️ No 5d high found for {symbol}, skipping BUY.", "WARNING", "BacktestEngine")
        #    continue

        #limit_pct = 1 if is_momentum else 1
        #price_limit = high_5d * limit_pct

        # get current price for comparison
        #df_check_price = get_price_data(symbol, days=1, interval="minute")
        #if df_check_price is None or df_check_price.empty:
        #    log(f"⚠️ No price data for {symbol} for 5d check, skipping.", "WARNING", "BacktestEngine")
        #    continue

        #current_price = df_check_price["close"].iloc[-1]

        #if current_price > price_limit:
        #    log(f"🚫 Skipping {symbol} BUY: current price {current_price:.2f} exceeds {limit_pct*100:.0f}% of 5d high ({high_5d:.2f}).", "INFO", "BacktestEngine")
        #    continue

        df = get_price_data(symbol, days=2, interval="minute")
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
        effective_confidence = 0.3 if is_momentum else MIN_CONFIDENCE
        if confidence < effective_confidence:
            continue

        price = best.get("close") or df["close"].iloc[-1]

        if is_momentum:
            max_capital = min(CAPITAL_LIMIT * 0.5, available_capital - total_pending_allocation)
            shares = max(int(max_capital // price), 0)
            capital_allocated = round(shares * price, 2)
        else:
            shares, capital_allocated = calculate_allocation_shares(symbol, price, confidence, available_capital - total_pending_allocation)

        if capital_allocated <= 0 or (total_pending_allocation + capital_allocated) > CAPITAL_LIMIT:
            log(f"⚠️ Skipping {symbol}, insufficient capital or cap exceeded.", "WARNING", "BacktestEngine")
            continue

        total_pending_allocation += capital_allocated
        if not is_momentum:
            sandbox_passed = simulate_sandbox(symbol)
            if not sandbox_passed:
                log(f"🚫 Skipping {symbol}, sandbox pre-check failed (non-momentum).", "WARNING", "BacktestEngine")
                continue
            
        order = {
            "symbol": symbol,
            "action": "BUY",
            "confidence": round(confidence, 3),
            "price": price,  # keep for reference
            "entry_price_triggered": price,  # NEW: explicitly save triggered price
            "shares": shares,
            "capital_allocated": capital_allocated,
            "timestamp": datetime.now(LOCAL_TZ).isoformat(),
            "allocation_notes": "Momentum" if is_momentum else ("Strong" if confidence >= 0.7 else "Weak")
        }
        orders.append(order)
        atomic_save_json(orders, ORDERS_PATH)
        log(f"{'📈 Momentum BUY' if is_momentum else '✅ BUY'} {symbol} @ {price} | {shares} shares | {order['allocation_notes']}", "INFO", "BacktestEngine")
        print(f"{'📈 Momentum BUY' if is_momentum else '✅ BUY'} {symbol} @ {price} | {shares} shares | {order['allocation_notes']}", "INFO", "BacktestEngine")
    save_json(control, CONTROL_PATH)
    save_json(recovery, RECOVERY_TRACK_FILE)
    save_json(last_exit_tracker, LAST_EXIT_TRACKER_PATH)
    update_system_status("backtest_engine", {"orders_generated": len(orders), "timestamp": datetime.now(LOCAL_TZ).isoformat()})
    log(f"Completed cycle with {len(orders)} orders.", "INFO", "BacktestEngine")