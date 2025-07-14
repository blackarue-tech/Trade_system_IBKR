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
from modules.Wallet_Utils import credit_wallet_balance
from polygon_utils import load_gainers_symbols, ensure_gainers_symbols, get_cached_5d_high
from polygon_utils import filter_gainers_for_momentum_score  # New helper to call scoring logic

LOCAL_TZ = pytz_timezone("America/Toronto")
us_holidays = holidays.US()
LIVE_PRICES_PATH = "exports/live_prices.json"
SELL_ORDERS_PATH = "exports/orders_sell.json"
BUY_ORDERS_PATH = "exports/orders_buy.json"
CONTROL_PATH = "inputs/ai_strategy_configs/control_params.json"
LIVE_TRADES_PATH = "exports/live_trades.json"
RECOVERY_TRACK_FILE = "exports/recovery_tracker.json"
WALLET_PATH = "exports/wallet.json"
LAST_EXIT_TRACKER_PATH = "exports/last_exit_tracker.json"
LIVE_PARAMS_PATH = "inputs/ai_strategy_configs/live_params.json"
EXIT_PARAMS_PATH = "inputs/ai_strategy_configs/exit_params.json"
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

def passes_momentum_sandbox(symbol):
    try:
        df = get_price_data(symbol, days=1, interval="minute")
        if df is None or df.empty or len(df) < 20:
            return False
        # Reuse same scoring logic used in filter_gainers_for_momentum
        score = filter_gainers_for_momentum_score(df)
        return score >= 4
    except Exception as e:
        log(f"Sandbox test failed for {symbol}: {e}", "WARNING", "BuyPhase")
        return False

def simulate_sandbox(symbol, passes=2, score_threshold=4):
    success_count = 0

    for _ in range(passes):
        df = get_price_data(symbol, days=1, interval="minute")
        if df is None or df.empty or len(df) < 20:
            continue

        df = add_technical_indicators(df)
        signals = get_strategy_signals(symbol, df)
        latest = df.iloc[-1]
        score = 0

        # === Price movement
        entry_price = df["close"].iloc[0]
        exit_price = df["close"].iloc[-1]
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        if pnl_pct > 0:
            score += 1

        # === RSI check
        if latest.get("rsi", 0) > 50:
            score += 1

        # === MACD histogram positive
        if latest.get("macd_hist", 0) > 0:
            score += 1

        # === Volume spike
        if latest.get("rvol", 0) > 1.3:
            score += 1

        # === Signal confidence check
        if not signals.empty and signals["confidence"].max() >= 0.6:
            score += 1

        if score >= score_threshold:
            success_count += 1

        time.sleep(0.1)

    passed = success_count >= (passes // 2 + 1)
    log(f"🧪 Sandbox for {symbol}: Passed={passed}, Successes={success_count}/{passes}", "DEBUG", "Sandbox")
    return passed

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
def generate_sell_orders():
    if not market_is_open():
        log("Market closed.", "INFO", "BacktestEngine")
        return

    now = datetime.now(LOCAL_TZ)
    if now.time() >= dtime(16, 0):
        log("🛑 Market closed at 16:00, skipping SELL generation.", "INFO", "BacktestEngine")
        return

    live_prices = load_json(LIVE_PRICES_PATH) if os.path.exists(LIVE_PRICES_PATH) else {}
    live_trades = load_json(LIVE_TRADES_PATH)
    last_exit_tracker = load_json(LAST_EXIT_TRACKER_PATH)
    exit_params = load_json(EXIT_PARAMS_PATH)
    live_params = load_json(LIVE_PARAMS_PATH)
    exit_failures = load_json("exports/exit_failures.json") if os.path.exists("exports/exit_failures.json") else []

    log("✅ Starting SELL checks on all open trades.", "INFO", "BacktestEngine")

    for day in live_trades.values():
        for trade in day.get("trades", []):
            if trade.get("status") != "open":
                continue

            symbol = trade.get("symbol")
            entry_price = trade.get("entry_price")
            shares = trade.get("shares")
            trade_entry_time_str = trade.get("datetime")
            strategies = trade.get("strategies_used", [])
            strategy = strategies[0] if strategies else "Momentum"

            if not entry_price or not shares or not trade_entry_time_str:
                log(f"⚠️ Incomplete trade data for {symbol}, skipping.", "WARNING", "BacktestEngine")
                continue

            df = get_price_data(symbol, days=2, interval="minute")
            if df is None or df.empty:
                log(f"⚠️ No price data for {symbol}, skipping SELL check.", "WARNING", "BacktestEngine")
                continue

            price = df["close"].iloc[-1]
            pnl_value = (price - entry_price) * shares
            pnl_pct = ((price - entry_price) / entry_price) * 100
            live_prices[symbol] = price

            trade_entry_time = datetime.fromisoformat(trade_entry_time_str)
            elapsed_secs = (datetime.now(LOCAL_TZ) - trade_entry_time).total_seconds()
            elapsed_minutes = elapsed_secs / 60

            strat_exit = exit_params.get(strategy, {})
            target_gain = strat_exit.get("target_gain", 0.015) * 100
            stop_loss = strat_exit.get("stop_loss", -0.015) * 100
            adaptive_hold = strat_exit.get("adaptive_hold_minutes", 180)
            min_hold_secs = strat_exit.get("min_hold_secs", 0)
            early_exit_rules = strat_exit.get("early_exit_rules", {})
            exit_signals_enabled = strat_exit.get("exit_signals", [])

            exit_reasons = []

            if elapsed_secs < min_hold_secs:
                log(f"⏳ Skipping {symbol}, held only {elapsed_secs:.1f}s < min_hold {min_hold_secs}s.", "DEBUG", "BacktestEngine")
                continue

            if pnl_pct >= target_gain:
                exit_reasons.append("target_gain_reached")
            if pnl_pct <= stop_loss:
                exit_reasons.append("stop_loss_triggered")
            if elapsed_minutes >= adaptive_hold:
                exit_reasons.append("adaptive_hold_exit")

            if early_exit_rules:
                profit_rule = early_exit_rules.get("profit")
                if profit_rule:
                    if pnl_pct >= profit_rule.get("profit_pct", 1.0) and elapsed_secs >= profit_rule.get("min_secs", 60):
                        exit_reasons.append("early_profit_take")
                loss_rule = early_exit_rules.get("loss")
                if loss_rule:
                    if pnl_pct <= loss_rule.get("loss_pct", -1.0) and elapsed_secs >= loss_rule.get("loss_wait_secs", 60):
                        exit_reasons.append("early_loss_cut")

            if exit_signals_enabled:
                indicators = add_technical_indicators(df.copy())
                last = indicators.iloc[-1]

                if "vwap_reversion" in exit_signals_enabled and last["close"] < last["vwap"]:
                    exit_reasons.append("vwap_reversion")
                if "rsi_drop" in exit_signals_enabled and last.get("rsi", 50) < 45:
                    exit_reasons.append("rsi_drop")
                if "macd_bearish" in exit_signals_enabled and last.get("macd_hist", 0) < 0:
                    exit_reasons.append("macd_bearish")
                if "bollinger_breakdown" in exit_signals_enabled and last["close"] < last.get("bb_lower", float('-inf')):
                    exit_reasons.append("bollinger_breakdown")
                if "atr_spike" in exit_signals_enabled and last.get("atr", 0) > df["atr"].rolling(20).mean().iloc[-1] * 1.5:
                    exit_reasons.append("atr_spike")

            if elapsed_secs > 1800 and pnl_pct >= 0.5:
                exit_reasons.append("time_exit_after_30min_small_pnl")
            log(f"🔎 Checking {symbol}: PnL {pnl_pct:.2f}%, target {target_gain}%, hold {elapsed_minutes:.1f}min / {adaptive_hold}min, exit_reasons: {exit_reasons}", "DEBUG", "BacktestEngine")
            if exit_reasons:
                if pnl_value < 0:
                    exit_failures.append({
                        "symbol": symbol,
                        "strategy": strategy,
                        "pnl": round(pnl_value, 2),
                        "elapsed_minutes": round(elapsed_minutes, 2),
                        "exit_reasons": exit_reasons,
                        "timestamp": datetime.now(LOCAL_TZ).isoformat()
                    })
                    log(f"🚫 Exit skipped due to negative PnL {pnl_value:.2f}. Logged for AI analysis.", "WARNING", "BacktestEngine")
                else:
                    # ✅ Immediate in-backtest execution
                    trade["exit_price"] = price
                    trade["exit_time"] = datetime.now(LOCAL_TZ).isoformat()
                    trade["pnl"] = round(pnl_value, 2)
                    trade["pnl_pct"] = round(pnl_pct, 2)
                    trade["status"] = "closed"
                    trade["exit_reasons"] = exit_reasons

                    credit_wallet_balance(WALLET_PATH, round(price * shares, 2))

                    last_exit_tracker[symbol] = {
                        "time": datetime.now(LOCAL_TZ).isoformat(),
                        "pnl": round(pnl_value, 2)
                    }
                    log(f"✅ CLOSED {symbol} @ {price:.2f} | PnL: {round(pnl_value, 2)} | Reasons: {exit_reasons}", "INFO", "BacktestEngine")
            else:
                log(f"🔄 Holding {symbol}, conditions for SELL not met.", "DEBUG", "BacktestEngine")

    save_json(exit_failures, "exports/exit_failures.json")
    save_json(last_exit_tracker, LAST_EXIT_TRACKER_PATH)
    atomic_save_json(live_trades, LIVE_TRADES_PATH)
    with open(LIVE_PRICES_PATH, "w") as f:
        json.dump(live_prices, f, indent=2)

    log("✅ Live prices updated and SELL checks completed.", "INFO", "BacktestEngine")


@fail_safe(module="BacktestEngine", critical=True)
def generate_buy_orders():
    if not market_is_open():
        log("Market closed.", "INFO", "BacktestEngine")
        return

    now = datetime.now(LOCAL_TZ)
    if now.time() >= dtime(16, 0):
        log("🛑 Market closed at 16:00, skipping BUY generation.", "INFO", "BacktestEngine")
        return

    live_params = load_json(LIVE_PARAMS_PATH)
    control = load_json(CONTROL_PATH)
    live_trades = load_json(LIVE_TRADES_PATH)
    recovery = load_json(RECOVERY_TRACK_FILE)

    min_confidence = live_params.get("min_confidence", 0.5)
    strategy_weights = live_params.get("strategy_weights", {})
    allocation_cfg = live_params.get("capital_allocation", {})
    base_pct = allocation_cfg.get("base_pct", 0.1)
    bonus_pct = allocation_cfg.get("bonus_pct", 0.2)
    max_allocation_pct = allocation_cfg.get("max_allocation_pct", 0.4)

    wallet_balance = get_wallet_balance()
    deployed = sum(
        trade.get("capital_used", 0)
        for day in live_trades.values()
        for trade in day.get("trades", [])
        if trade.get("status") == "open"
    )
    available_capital = max(0, min(wallet_balance, CAPITAL_LIMIT - deployed))
    total_pending_allocation = 0

    momentum_symbols, non_momentum_symbols = load_gainers_symbols()
    if not momentum_symbols and not non_momentum_symbols:
        log("❌ No symbols available to trade from top_gainers.json.", "ERROR", "BacktestEngine")
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
            log(f"✅ Using {len(symbols_to_trade)} non-momentum symbols.", "INFO", "BacktestEngine")
        else:
            log(f"⚠️ No slots available for non-momentum symbols.", "WARNING", "BacktestEngine")
            return

    log("✅ Starting BUY checks and order generation.", "INFO", "BacktestEngine")

    for symbol in symbols_to_trade:
        is_momentum = symbol in momentum_symbols

        now = datetime.now(LOCAL_TZ)
        if now.time() >= dtime(15, 30):
            log(f"🛑 Skipping BUY for {symbol} after 15:30 cutoff.", "INFO", "BacktestEngine")
            continue
        if not is_momentum and not simulate_sandbox(symbol):
            log(f"🚫 Skipping {symbol}, sandbox pre-buy check failed (non-momentum).", "WARNING", "BacktestEngine")
            continue
        if is_momentum and not passes_momentum_sandbox(symbol):
            log(f"⛔ {symbol} failed sandbox momentum check — skipping.", "WARNING", "BacktestEngine")
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
        raw_confidence = best["hybrid_confidence"]
        strategies = best.get("strategies", [])
        strategy = strategies[0] if strategies else "Momentum"
        weight = strategy_weights.get(strategy, 1.0)
        confidence = raw_confidence * weight

        effective_confidence = 0.3 if is_momentum else min_confidence
        if confidence < effective_confidence:
            log(f"⚠️ Skipping {symbol}, confidence {confidence:.2f} below threshold {effective_confidence:.2f}.", "DEBUG", "BacktestEngine")
            continue

        price = best.get("close") or df["close"].iloc[-1]

        allocation_pct = min(base_pct + bonus_pct * confidence, max_allocation_pct)
        max_capital = min(available_capital - total_pending_allocation, CAPITAL_LIMIT * allocation_pct)
        shares = max(int(max_capital // price), 0)
        capital_allocated = round(shares * price, 2)

        if shares < 1 or capital_allocated <= 0:
            log(f"⚠️ Skipping {symbol}, insufficient capital for allocation.", "WARNING", "BacktestEngine")
            continue

        total_pending_allocation += capital_allocated

        if not is_momentum and not simulate_sandbox(symbol):
            log(f"🚫 Skipping {symbol}, sandbox pre-check failed.", "WARNING", "BacktestEngine")
            continue

        buy_order = {
            "symbol": symbol,
            "action": "BUY",
            "confidence": round(confidence, 3),
            "price": price,
            "entry_price_triggered": price,
            "shares": shares,
            "capital_allocated": capital_allocated,
            "timestamp": datetime.now(LOCAL_TZ).isoformat(),
            "strategies_used": [strategy],  # << 🔥 Add this line
            "allocation_notes": "Momentum" if is_momentum else ("Strong" if confidence >= 0.7 else "Weak")
        }

        atomic_save_json(buy_order, BUY_ORDERS_PATH)
        log(f"{'📈 Momentum BUY' if is_momentum else '✅ BUY'} {symbol} @ {price:.2f} | {shares} shares", "INFO", "BacktestEngine")

    save_json(control, CONTROL_PATH)
    save_json(recovery, RECOVERY_TRACK_FILE)
    update_system_status("backtest_engine", {
        "status": "Orders processing complete",
        "timestamp": datetime.now(LOCAL_TZ).isoformat()
    })
    log(f"Completed BUY checks.", "INFO", "BacktestEngine")
    
# === LOOP ===
def run_loop():
    while True:
        generate_sell_orders()
        generate_buy_orders()        
        log(f"Sleeping {LOOP_INTERVAL}s before next cycle.", "INFO", "BacktestEngine")        
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    run_loop()
