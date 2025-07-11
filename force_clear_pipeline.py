# force_clear_pipeline.py - Forced pipeline clearing SELL for monitored symbols

import os
import json
from datetime import datetime
from pytz import timezone
from modules.questrade import get_quote, get_symbol_id
from utils import atomic_save_json
from modules.log_utils import log

LOCAL_TZ = timezone("America/Toronto")
ORDERS_PATH = "exports/backtest_orders.json"
LIVE_TRADES_PATH = "exports/live_trades.json"
LAST_EXIT_TRACKER_PATH = "exports/last_exit_tracker.json"
MONITORED_SYMBOLS = ["ABVC", "BMNR", "PTHS", "SRFM"]

def load_live_trades():
    if os.path.exists(LIVE_TRADES_PATH):
        with open(LIVE_TRADES_PATH) as f:
            return json.load(f)
    return {}

def save_order(sell_order):
    orders = []
    if os.path.exists(ORDERS_PATH):
        with open(ORDERS_PATH) as f:
            orders = json.load(f)
    orders.append(sell_order)
    atomic_save_json(orders, ORDERS_PATH)

def update_last_exit_tracker(symbol, pnl_value):
    last_exit_tracker = {}
    if os.path.exists(LAST_EXIT_TRACKER_PATH):
        with open(LAST_EXIT_TRACKER_PATH) as f:
            last_exit_tracker = json.load(f)
    last_exit_tracker[symbol] = {
        "time": datetime.now(LOCAL_TZ).isoformat(),
        "pnl": round(pnl_value, 2)
    }
    atomic_save_json(last_exit_tracker, LAST_EXIT_TRACKER_PATH)

def force_clear_pipeline():
    trades = load_live_trades()
    now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")

    for symbol in MONITORED_SYMBOLS:
        for day_trades in trades.values():
            for trade in day_trades.get("trades", []):
                if trade.get("status") == "open" and trade.get("symbol") == symbol:
                    entry_price = trade.get("entry_price")
                    shares = trade.get("shares")
                    symbol_id = get_symbol_id(symbol)
                    quote = get_quote(symbol_id)
                    price = quote.get("lastTradePrice")

                    if price is None or entry_price is None or shares is None:
                        continue

                    pnl_value = (price - entry_price) * shares
                    pnl_pct = ((price - entry_price) / entry_price) * 100

                    log_line = (
                        f"[{now_str}] FORCED SELL {symbol} | Entry: {entry_price:.2f} | "
                        f"Current: {price:.2f} | PnL: {pnl_value:.2f} ({pnl_pct:.2f}%)"
                    )
                    print(log_line)
                    log(log_line, "INFO", "ForceClear")

                    sell_order = {
                        "symbol": symbol,
                        "action": "SELL",
                        "price": price,
                        "pnl": round(pnl_value, 2),
                        "timestamp": datetime.now(LOCAL_TZ).isoformat(),
                        "exit_reasons": ["forced_clear_pipeline"],
                        "allocation_notes": "Forced Clear"
                    }
                    save_order(sell_order)
                    update_last_exit_tracker(symbol, pnl_value)

                    confirm_msg = (
                        f"✅ FORCED SELL ORDER PLACED for {symbol} at {price:.2f} | "
                        f"PnL: {pnl_value:.2f} ({pnl_pct:.2f}%)"
                    )
                    print(confirm_msg)
                    log(confirm_msg, "INFO", "ForceClear")

if __name__ == "__main__":
    force_clear_pipeline()

