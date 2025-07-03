# live_executor.py - Now clears orders.json after execution and skips duplicate buys

import os
import json
import time
from datetime import datetime, time as dtime
from pytz import timezone
import holidays
from modules.questrade import get_quote, get_symbol_id
from modules.Wallet_Utils import debit_wallet_balance
from utils import atomic_save_json, update_system_status

ORDERS_PATH = "exports/backtest_orders.json"
LIVE_TRADES_PATH = "exports/live_trades.json"
WALLET_PATH = "exports/wallet.json"
LOOP_INTERVAL = 10
LOCAL_TZ = timezone("America/Toronto")
us_holidays = holidays.US()

def market_is_open():
    now = datetime.now(LOCAL_TZ)
    if now.date() in us_holidays:
        return False
    return now.weekday() < 5 and dtime(9, 30) <= now.time() <= dtime(16, 0)

def load_orders():
    if os.path.exists(ORDERS_PATH):
        with open(ORDERS_PATH) as f:
            return json.load(f)
    return []

def clear_orders():
    atomic_save_json([], ORDERS_PATH)

def load_live_trades():
    if os.path.exists(LIVE_TRADES_PATH):
        with open(LIVE_TRADES_PATH) as f:
            return json.load(f)
    return {}

def save_live_trades(trades):
    atomic_save_json(trades, LIVE_TRADES_PATH)

def execute_buys():
    if not market_is_open():
        print(f"[{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] 🛑 Market closed, skipping BUY execution.")
        return

    orders = load_orders()
    trades = load_live_trades()
    now = datetime.now(LOCAL_TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    today_key = now.strftime("%Y-%m-%d")

    if today_key not in trades:
        trades[today_key] = {"trades": []}

    updated = False
    for order in orders:
        if order.get("action") != "BUY":
            continue

        symbol = order["symbol"]

        # Prevent duplicate buys if already in trade
        already_in_trade = any(
            trade.get("status") == "open" and trade.get("symbol") == symbol
            for day in trades.values()
            for trade in day.get("trades", [])
        )
        if already_in_trade:
            print(f"[{now_str}] ⚠️ Skipping {symbol}, already in trade.")
            continue

        symbol_id = get_symbol_id(symbol)
        quote = get_quote(symbol_id)
        price = quote.get("lastTradePrice")
        if price is None:
            print(f"[{now_str}] ⚠️ No price for {symbol}, skipping execution.")
            continue

        capital_to_use = order.get("capital_allocated", 1000)
        shares = int(capital_to_use // price)
        if shares < 1:
            print(f"[{now_str}] ⚠️ Insufficient capital to buy shares for {symbol}.")
            continue

        total_cost = round(shares * price, 2)
        if not debit_wallet_balance(WALLET_PATH, total_cost):
            print(f"[{now_str}] ⚠️ Wallet debit failed for {symbol}.")
            continue

        trade = {
            "symbol": symbol,
            "symbolId": symbol_id,
            "datetime": now.isoformat(),
            "entry_price": price,
            "shares": shares,
            "capital_used": total_cost,
            "status": "open",
            "confidence": order.get("confidence", 0),
            "allocation_notes": order.get("allocation_notes", "backtest_engine")
        }

        trades[today_key]["trades"].append(trade)
        updated = True

        print(f"[{now_str}] 🚀 Executed BUY {shares}x {symbol} @ {price:.2f} | Allocated: ${total_cost:.2f}")

    if updated:
        save_live_trades(trades)
        clear_orders()  # Clear orders after executing to prevent re-execution
        update_system_status("live_executor", {"last_execution": now.isoformat()})

def run_loop():
    while True:
        execute_buys()
        print(f"[{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] ⏱️ Sleeping {LOOP_INTERVAL}s before checking for BUY orders.")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()
