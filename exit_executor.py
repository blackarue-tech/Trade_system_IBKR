# exit_executor.py – Modular Strategy Exit Framework with Adaptive Strategy Rules

import os, json, time
from datetime import datetime
import pytz
from dateutil.parser import isoparse
import pandas as pd
from modules.questrade import get_quote
from modules.price_data import get_price_data
from modules.Wallet_Utils import credit_wallet_balance
from modules.model_utils import add_technical_indicators

LIVE_TRADES_PATH = "exports/live_trades.json"
WALLET_PATH = "exports/wallet.json"
RULES_PATH = "inputs/strategy_exit_rules.json"
LOOP_INTERVAL = 30
LOCAL_TZ = pytz.timezone("America/Toronto")

def get_market_now():
    return datetime.now(LOCAL_TZ)

def load_live_trades():
    if os.path.exists(LIVE_TRADES_PATH):
        with open(LIVE_TRADES_PATH, "r") as f:
            return json.load(f)
    return {}

def save_live_trades(trades):
    with open(LIVE_TRADES_PATH, "w") as f:
        json.dump(trades, f, indent=2)

def load_exit_rules():
    with open(RULES_PATH) as f:
        return json.load(f)

EXIT_RULES = load_exit_rules()

def get_primary_strategy(trade):
    return trade.get("strategies_used", ["Unknown"])[0]

def get_rule(trade, key, default):
    strategy = get_primary_strategy(trade)
    return EXIT_RULES.get(strategy, {}).get(key, default)

def strategy_exit_check(trade, current_price):
    symbol_id = trade["symbolId"]
    df = get_price_data(symbol_id, days=1, interval="OneMinute")
    if df is None or df.empty:
        return None, None
    df = add_technical_indicators(df)
    latest = df.iloc[-1]

    strategy = get_primary_strategy(trade)
    signals = get_rule(trade, "exit_signals", [])

    if "vwap_reversion" in signals and latest["close"] < latest["vwap"]:
        return "vwap_reversion", current_price
    if "rsi_drop" in signals and latest.get("rsi", 100) < 50:
        return "rsi_drop", current_price
    if "breakout_fail" in signals and latest["close"] < trade["entry_price"]:
        return "breakout_fail", current_price
    if "macd_bearish" in signals and latest.get("macd_hist", 0) < 0:
        return "macd_bearish", current_price
    if "ma_breakdown" in signals and latest["close"] < latest.get("ema20", latest["close"]):
        return "ma_breakdown", current_price

    return None, None

def trailing_stop_check(trade, current_price):
    trailing_pct = get_rule(trade, "trailing_stop_pct", 0.01)
    peak = trade.get("peak_price", trade["entry_price"])
    peak = max(peak, current_price)
    trade["peak_price"] = peak
    if current_price < peak * (1 - trailing_pct):
        return "trailing_stop", current_price
    return None, None

def check_exit_conditions(trade, current_price):
    entry_price = trade["entry_price"]
    entry_time = isoparse(trade["datetime"])
    now = get_market_now()
    time_held_min = (now - entry_time).total_seconds() / 60

    pnl_pct = (current_price - entry_price) / entry_price

    min_hold = get_rule(trade, "min_hold_min", 30)
    if time_held_min < min_hold:
        return None, None

    strat_reason, strat_exit = strategy_exit_check(trade, current_price)
    if strat_exit:
        return strat_reason, strat_exit

    trail_reason, trail_exit = trailing_stop_check(trade, current_price)
    if trail_exit:
        return trail_reason, trail_exit

    if pnl_pct >= 0.015:
        return "target", current_price
    elif pnl_pct <= -0.015:
        return "stop", current_price

    return None, None

def exit_executor_loop():
    while True:
        print("🔄 Checking open trades...")
        trades = load_live_trades()
        today = get_market_now().strftime("%Y-%m-%d")
        updated = False

        if today not in trades:
            time.sleep(LOOP_INTERVAL)
            continue

        for trade in trades[today]["trades"]:
            if trade.get("exit_price"):
                continue

            symbol = trade["symbol"]
            symbol_id = trade["symbolId"]
            quantity = trade["shares"]
            print(f"🔍 Checking {symbol} with symbolId: {symbol_id}")

            try:
                quote = get_quote(symbol_id)
            except Exception as e:
                print(f"⚠️ Quote fetch failed for {symbol}: {e}")
                continue

            current_price = (
                quote.get("last") or
                quote.get("lastTradePrice") or
                quote.get("lastTradePriceTrHrs") or
                quote.get("bidPrice")
            )

            if current_price is None:
                print(f"⚠️ No valid price for {symbol}, skipping...")
                continue

            exit_reason, exit_price = check_exit_conditions(trade, current_price)

            if exit_price:
                trade["exit_price"] = exit_price
                trade["exit_time"] = get_market_now().isoformat()
                trade["exit_reason"] = exit_reason
                trade["pnl"] = (exit_price - trade["entry_price"]) * quantity
                trade["pnl_pct"] = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100
                trade["status"] = "closed"
                credit_wallet_balance(WALLET_PATH, exit_price * quantity)
                updated = True

                time_held = (get_market_now() - isoparse(trade["datetime"])).total_seconds()
                print(f"✅ Closed {symbol} - {exit_reason} @ {exit_price:.2f} | PnL: {trade['pnl']:.2f}")
                print(f"⏱️ Held {time_held:.1f}s | PnL: {trade['pnl_pct']:.4f}% | {symbol}")

        if updated:
            save_live_trades(trades)

        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    exit_executor_loop()