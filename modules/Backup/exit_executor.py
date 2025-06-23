
# exit_executor.py – Modular Strategy Exit Framework with Resilient Token Handling

import os, json, time
from datetime import datetime, timedelta
import pytz
from dateutil.parser import isoparse
import pandas as pd
from modules.questrade import get_quote  # Uses safe_questrade_request internally
from modules.price_data import get_price_data
from modules.Wallet_Utils import credit_wallet_balance
from modules.model_utils import add_technical_indicators

LIVE_TRADES_PATH = "exports/live_trades.json"
WALLET_PATH = "exports/wallet.json"
EXIT_HOLD_MINUTES = 30
TARGET_GAIN = 0.015
STOP_LOSS = -0.015
TRAIL_PCT = 0.01  # 1% trailing stop
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

def strategy_exit_check(trade, current_price):
    symbol_id = trade["symbolId"]
    df = get_price_data(symbol_id, days=1, interval="OneMinute")
    if df is None or df.empty:
        return None, None
    df = add_technical_indicators(df)
    latest = df.iloc[-1]

    strat_list = trade.get("strategies_used", [])

    if "VWAP_Bounce" in strat_list:
        if latest["close"] < latest["vwap"]:
            return "vwap_reversion", current_price

    if "Momentum" in strat_list:
        if latest.get("rsi", 100) < 50:
            return "rsi_drop", current_price

    if "Breakout" in strat_list:
        entry_price = trade["entry_price"]
        if latest["close"] < entry_price:
            return "breakout_fail", current_price

    if "MACD" in strat_list:
        if latest.get("macd_hist", 0) < 0:
            return "macd_bearish", current_price

    if "MovingAverage" in strat_list:
        if latest["close"] < latest.get("ema20", latest["close"]):
            return "ma_breakdown", current_price

    return None, None

def trailing_stop_check(trade, current_price):
    peak = trade.get("peak_price", trade["entry_price"])
    peak = max(peak, current_price)
    trade["peak_price"] = peak
    if current_price < peak * (1 - TRAIL_PCT):
        return "trailing_stop", current_price
    return None, None

def get_adaptive_hold_time(trade):
    base_hold = 240  # minutes (4 hours)
    strategy = trade.get("strategies_used", [""])[0]
    confidence = trade.get("confidence", 0.5)

    if strategy == "Momentum" and confidence > 0.8:
        return 90
    elif strategy == "Breakout" and confidence > 0.75:
        return 120
    elif strategy == "VWAP_Bounce" and confidence < 0.6:
        return 300
    elif strategy == "MACD" and confidence >= 0.7:
        return 180
    else:
        return base_hold

def check_exit_conditions(trade, current_price):
    entry_price = trade["entry_price"]
    entry_time = isoparse(trade["datetime"])
    now = get_market_now()

    time_held_sec = (now - entry_time).total_seconds()
    pnl_pct = (current_price - entry_price) / entry_price

    strat_reason, strat_exit = strategy_exit_check(trade, current_price)
    if strat_exit:
        return strat_reason, strat_exit

    trail_reason, trail_exit = trailing_stop_check(trade, current_price)
    if trail_exit:
        return trail_reason, trail_exit

    adaptive_hold_min = get_adaptive_hold_time(trade)
    if time_held_sec >= adaptive_hold_min * 60:
        return "adaptive_timer", current_price

    if pnl_pct >= TARGET_GAIN:
        return "target", current_price
    elif pnl_pct <= STOP_LOSS:
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
                quote = get_quote(symbol_id)  # uses safe_questrade_request internally
            except Exception as e:
                print(f"⚠️ Quote fetch failed for {symbol}: {e}")
                continue

            print(f"📦 Raw quote for {symbol}: {quote}")
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

                entry_time = isoparse(trade["datetime"])
                now = get_market_now()
                time_held_sec = (now - entry_time).total_seconds()
                pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100
                print(f"✅ Closed {symbol} - {exit_reason} @ {exit_price:.2f} | PnL: {trade['pnl']:.2f}")
                print(f"⏱️ Held {time_held_sec:.1f}s | PnL: {pnl_pct:.4f} | {trade['symbol']}")

        if updated:
            save_live_trades(trades)

        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    exit_executor_loop()
