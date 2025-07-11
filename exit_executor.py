# exit_executor.py - Now respects US market hours, stable PnL tracking, updates last_exit_tracker correctly

import os
import json
import time
from datetime import datetime, time as dtime
from pytz import timezone
import holidays
from modules.questrade import get_quote, get_symbol_id
from modules.Wallet_Utils import credit_wallet_balance
from utils import atomic_save_json, update_system_status
from modules.log_utils import log, fail_safe

ORDERS_PATH = "exports/orders_sell.json"
LIVE_TRADES_PATH = "exports/live_trades.json"
WALLET_PATH = "exports/wallet.json"
LAST_EXIT_TRACKER_PATH = "exports/last_exit_tracker.json"
LOOP_INTERVAL = 1
LOCAL_TZ = timezone("America/Toronto")
MAX_SLIPPAGE_PCT = 0.5
us_holidays = holidays.US()

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
        log(f"Error loading JSON from {path}: {e}", "ERROR", "ExitExecutor")
    return {}

def save_json(data, path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Error saving JSON to {path}: {e}", "ERROR", "ExitExecutor")

def atomic_save_live_trades(trades):
    try:
        atomic_save_json(trades, LIVE_TRADES_PATH)
    except Exception as e:
        log(f"Error saving live trades: {e}", "ERROR", "ExitExecutor")
    
@fail_safe(module="ExitExecutor", critical=True)
def execute_exits():
    if not os.path.exists(ORDERS_PATH):
        return

    if not market_is_open():
        log("Market closed, skipping SELL execution.", "INFO", "ExitExecutor")
        return

    try:
        with open(ORDERS_PATH) as f:
            orders = json.load(f)
        if isinstance(orders, dict):
            orders = [orders]
    except Exception as e:
        log(f"Error reading {ORDERS_PATH}: {e}", "ERROR", "ExitExecutor")
        return

    with open(LIVE_TRADES_PATH) as f:
        trades = json.load(f) if os.path.exists(LIVE_TRADES_PATH) else {}

    now = datetime.now(LOCAL_TZ)
    updated = False
    remaining_orders = []

    for order in orders:
        if order.get("action") != "SELL":
            remaining_orders.append(order)
            continue

        symbol = order["symbol"]
        triggered_price = order.get("price")

        symbol_id = get_symbol_id(symbol)
        quote = get_quote(symbol_id)
        executed_price = quote.get("lastTradePrice")

        if executed_price is None or triggered_price is None:
            log(f"Skipping {symbol}, missing price data for SELL.", "WARNING", "ExitExecutor")
            continue

        slippage_pct = ((executed_price - triggered_price) / triggered_price) * 100

        #if slippage_pct < -MAX_SLIPPAGE_PCT:
            #log(f"🚫 Skipping SELL {symbol}: slippage {slippage_pct:.2f}% exceeds -{MAX_SLIPPAGE_PCT:.2f}% limit.", "WARNING", "ExitExecutor")
            #continue  # reject excessive slippage

        for date_key, day_trades in trades.items():
            for trade in day_trades.get("trades", []):
                if trade.get("status") == "open" and trade.get("symbol") == symbol:
                    entry_price = trade["entry_price"]
                    
                    # ✅ Positive PnL enforcement before executing SELL
                    pnl_value_check = (executed_price - entry_price) * trade["shares"]
                    if pnl_value_check <= 0:
                        log(f"🚫 Skipping SELL {symbol}: PnL would be negative (${pnl_value_check:.2f}), keeping trade open.", "WARNING", "ExitExecutor")
                        continue  # skip execution, do not re-add to remaining_orders
                    
                    shares = trade["shares"]
                    pnl_value = round((executed_price - entry_price) * shares, 2)

                    trade["exit_price"] = executed_price
                    trade["exit_price_triggered"] = triggered_price
                    trade["exit_price_executed"] = executed_price
                    trade["exit_time"] = now.isoformat()
                    trade["pnl"] = pnl_value
                    trade["pnl_pct"] = round((executed_price - entry_price) / entry_price * 100, 2)
                    trade["status"] = "closed"
                    trade["exit_reasons"] = order.get("exit_reasons", [])

                    credit_wallet_balance(WALLET_PATH, round(executed_price * shares, 2))

                    # Update last exit tracker
                    last_exit_tracker = load_json(LAST_EXIT_TRACKER_PATH)
                    last_exit_tracker[symbol] = {
                        "time": now.isoformat(),
                        "pnl": pnl_value
                    }
                    atomic_save_json(last_exit_tracker, LAST_EXIT_TRACKER_PATH)

                    log(f"✅ Executed SELL {symbol} @ {executed_price:.2f} (triggered {triggered_price:.2f}) | Slippage {slippage_pct:.2f}% | PnL: {pnl_value:.2f}", "INFO", "ExitExecutor")
                    updated = True

    if updated:
        atomic_save_json(trades, LIVE_TRADES_PATH)

    atomic_save_json(remaining_orders, ORDERS_PATH)
    update_system_status("exit_executor", {"last_execution": now.isoformat()})

def run_loop():
    while True:
        execute_exits()
        #log(f"Sleeping {LOOP_INTERVAL}s before checking for SELL orders.", "INFO", "ExitExecutor")
        #print(f"[{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] ⏱️ Sleeping {LOOP_INTERVAL}s beforeS checking for SELL orders.")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()
