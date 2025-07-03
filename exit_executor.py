# exit_executor.py - Now respects US market hours, stable PnL tracking, updates last_exit_tracker correctly

import os
import json
import time
from datetime import datetime, time as dtime
from pytz import timezone
import holidays
from modules.questrade import get_quote
from modules.Wallet_Utils import credit_wallet_balance
from utils import atomic_save_json, update_system_status

ORDERS_PATH = "exports/backtest_orders.json"
LIVE_TRADES_PATH = "exports/live_trades.json"
WALLET_PATH = "exports/wallet.json"
LAST_EXIT_TRACKER_PATH = "exports/last_exit_tracker.json"
LOOP_INTERVAL = 10
LOCAL_TZ = timezone("America/Toronto")
us_holidays = holidays.US()

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

def atomic_save_live_trades(trades):
    atomic_save_json(trades, LIVE_TRADES_PATH)

def execute_exits():
    if not market_is_open():
        print(f"[{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] 🛑 Market closed, skipping exit checks.")
        return

    orders = load_json(ORDERS_PATH)
    trades = load_json(LIVE_TRADES_PATH)
    last_exit_tracker = load_json(LAST_EXIT_TRACKER_PATH)
    now = datetime.now(LOCAL_TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S %Z")

    updated = False

    for order in orders:
        if order.get("action") != "SELL":
            continue

        symbol = order["symbol"]
        price = order["price"]
        exit_reasons = order.get("exit_reasons", [])

        for date_key, day_trades in trades.items():
            for trade in day_trades.get("trades", []):
                if trade.get("status") == "open" and trade.get("symbol") == symbol:
                    entry_price = trade["entry_price"]
                    shares = trade["shares"]
                    pnl_value = round((price - entry_price) * shares, 2)

                    trade["exit_price"] = price
                    trade["exit_time"] = now.isoformat()
                    trade["pnl"] = pnl_value
                    trade["pnl_pct"] = round((price - entry_price) / entry_price * 100, 2)
                    trade["status"] = "closed"
                    trade["exit_reasons"] = exit_reasons

                    credit_wallet_balance(WALLET_PATH, round(price * shares, 2))

                    # Update last_exit_tracker with PnL and timestamp
                    last_exit_tracker[symbol] = {
                        "time": now.isoformat(),
                        "pnl": pnl_value
                    }
                    save_json(last_exit_tracker, LAST_EXIT_TRACKER_PATH)

                    print(f"[{now_str}] ✅ Executed SELL for {symbol} @ {price:.2f} | PnL: {pnl_value:.2f} | Reasons: {', '.join(exit_reasons)}")
                    updated = True

    if updated:
        atomic_save_live_trades(trades)
        update_system_status("exit_executor", {"last_exit": now.isoformat()})

def run_loop():
    while True:
        execute_exits()
        print(f"[{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] ⏱️ Sleeping {LOOP_INTERVAL}s before checking for SELL orders.")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()
