# live_executor.py - Now prevents duplicate buys within same batch and across cycles

import os
import json
import time
from datetime import datetime, time as dtime
from pytz import timezone
import holidays
from modules.questrade import get_quote, get_symbol_id
from modules.Wallet_Utils import debit_wallet_balance
from utils import atomic_save_json, update_system_status
from modules.log_utils import log, fail_safe

ORDERS_PATH = "exports/orders_buy.json"
LIVE_TRADES_PATH = "exports/live_trades.json"
WALLET_PATH = "exports/wallet.json"
LOOP_INTERVAL = 1
LOCAL_TZ = timezone("America/Toronto")
MAX_SLIPPAGE_PCT = 2
us_holidays = holidays.US()

def market_is_open():
    now = datetime.now(LOCAL_TZ)
    if now.date() in us_holidays:
        return False
    return now.weekday() < 5 and dtime(9, 30) <= now.time() <= dtime(16, 0)

def load_orders():
    try:
        if os.path.exists(ORDERS_PATH):
            with open(ORDERS_PATH) as f:
                return json.load(f)
    except Exception as e:
        log(f"Error loading orders: {e}", "ERROR", "LiveExecutor")
    return []

def clear_orders():
    try:
        atomic_save_json([], ORDERS_PATH)
    except Exception as e:
        log(f"Error clearing orders: {e}", "ERROR", "LiveExecutor")

def load_live_trades():
    try:
        if os.path.exists(LIVE_TRADES_PATH):
            with open(LIVE_TRADES_PATH) as f:
                return json.load(f)
    except Exception as e:
        log(f"Error loading live trades: {e}", "ERROR", "LiveExecutor")
    return {}

def save_live_trades(trades):
    try:
        atomic_save_json(trades, LIVE_TRADES_PATH)
    except Exception as e:
        log(f"Error saving live trades: {e}", "ERROR", "LiveExecutor")

@fail_safe(module="LiveExecutor", critical=True)
def execute_buys():
    if not market_is_open():
        log("Market closed, skipping BUY execution.", "INFO", "LiveExecutor")
        return

    if not os.path.exists(ORDERS_PATH):
        return

    try:
        with open(ORDERS_PATH) as f:
            orders = json.load(f)
        if isinstance(orders, dict):
            # If accidentally saved as a single dict, convert to list
            orders = [orders]
    except Exception as e:
        log(f"Error reading {ORDERS_PATH}: {e}", "ERROR", "LiveExecutor")
        return

    trades = load_live_trades()

    now = datetime.now(LOCAL_TZ)
    today_key = now.strftime("%Y-%m-%d")
    if today_key not in trades:
        trades[today_key] = {"trades": []}

    updated = False
    remaining_orders = []
    processed_symbols = set()

    for order in orders:
        if order.get("action") != "BUY":
            remaining_orders.append(order)
            continue

        symbol = order["symbol"]

        # Prevent duplicate buys within the same batch
        if symbol in processed_symbols:
            log(f"Skipping duplicate BUY for {symbol} in same batch.", "WARNING", "LiveExecutor")
            continue

        # Check if already in open trade
        already_in_trade = any(
            trade.get("status") == "open" and trade.get("symbol") == symbol
            for day in trades.values()
            for trade in day.get("trades", [])
        )
        if already_in_trade:
            log(f"Skipping {symbol}, already in open trade.", "WARNING", "LiveExecutor")
            continue

        processed_symbols.add(symbol)

        triggered_price = order.get("price")
        symbol_id = get_symbol_id(symbol)
        quote = get_quote(symbol_id)
        executed_price = quote.get("lastTradePrice")

        if executed_price is None or triggered_price is None:
            log(f"Skipping {symbol}, missing price data.", "WARNING", "LiveExecutor")
            continue

        slippage_pct = ((executed_price - triggered_price) / triggered_price) * 100

        if slippage_pct > MAX_SLIPPAGE_PCT:
            log(f"🚫 Skipping BUY {symbol}: slippage {slippage_pct:.2f}% exceeds {MAX_SLIPPAGE_PCT:.2f}%.", "WARNING", "LiveExecutor")
            continue

        capital_to_use = order.get("capital_allocated", 1000)
        shares = int(capital_to_use // executed_price)
        if shares < 1:
            log(f"Insufficient capital to buy shares for {symbol}.", "WARNING", "LiveExecutor")
            continue

        total_cost = round(shares * executed_price, 2)
        if not debit_wallet_balance(WALLET_PATH, total_cost):
            log(f"Wallet debit failed for {symbol}.", "WARNING", "LiveExecutor")
            continue

        trade = {
            "symbol": symbol,
            "symbolId": symbol_id,
            "datetime": now.isoformat(),
            "entry_price": executed_price,
            "entry_price_triggered": triggered_price,
            "entry_price_executed": executed_price,
            "shares": shares,
            "capital_used": total_cost,
            "status": "open",
            "confidence": order.get("confidence", 0),
            "allocation_notes": order.get("allocation_notes", "backtest_engine")
        }

        trades[today_key]["trades"].append(trade)
        updated = True
        log(f"🚀 Executed BUY {shares}x {symbol} @ {executed_price:.2f} (triggered {triggered_price:.2f}) | Slippage {slippage_pct:.2f}% | Allocated: ${total_cost:.2f}", "INFO", "LiveExecutor")

    if updated:
        save_live_trades(trades)

    atomic_save_json(remaining_orders, ORDERS_PATH)
    update_system_status("live_executor", {"last_execution": now.isoformat()})

def run_loop():
    while True:
        execute_buys()
        #log(f"Sleeping {LOOP_INTERVAL}s before checking for BUY orders.", "INFO", "LiveExecutor")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()
