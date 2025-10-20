import asyncio
from datetime import datetime
from pytz import timezone as pytz_timezone
import time
import os
import json
import pandas as pd
import traceback

import pytz
from ib_insync import IB, Stock, MarketOrder, util
from utils import atomic_save_json
from modules.log_utils import log

# === Config ===
IBKR_HOST = '127.0.0.1'
IBKR_PORT = 4002  # Use 4001 for live TWS or 4002 for paper trading
LOCAL_TZ = pytz_timezone("America/Toronto")
IBKR_ORDER_LOG = "exports/ibkr_orders_log.json"

# === Global Shared IBKR Instance ===
ib = IB()
CLIENT_ID = 102  # Default fallback, will be overridden

def set_client_id(cid: int):
    global CLIENT_ID
    CLIENT_ID = cid
    print(f"🔧 IBKR client ID set to {cid}")
    
# === Market Data Fetching ===
def get_ibkr_quote(symbol: str, currency: str = "USD") -> dict:
    if not connect_ibkr():
        return {"error": "IBKR not connected"}

    try:
        contract = Stock(symbol, "SMART", currency)
        ib.qualifyContracts(contract)

        # If no market data ID is returned, it's not resolved properly
        if not contract.conId:
            print(f"⚠️ {symbol} contract could not be resolved.")
            return {"error": "Contract not resolved"}

        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(1.5)
        ib.cancelMktData(contract)

        quote = {
            "symbol": symbol,
            "bidPrice": ticker.bid,
            "askPrice": ticker.ask,
            "lastTradePrice": ticker.last,
            "volume": ticker.volume,
            "timestamp": datetime.now(LOCAL_TZ).isoformat()
        }
        print(f"📥 IBKR quote for {symbol}: {quote}")
        return quote

    except Exception as e:
        print(f"❌ Error fetching IBKR quote for {symbol}: {e}")
        return {"error": str(e), "symbol": symbol}



# === Safe Connection Logic ===
def connect_ibkr() -> bool:
    if ib.isConnected():
        return True
    try:
        print(f"🔌 Attempting IBKR connection on {IBKR_HOST}:{IBKR_PORT} with clientId={CLIENT_ID}")
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=CLIENT_ID, timeout=5)
        if ib.isConnected():
            print("✅ Connected to IBKR API.")
            return True
        else:
            print("❌ IBKR connection failed.")
            return False
    except Exception as e:
        print(f"❌ Exception during IBKR connect(): {e}")
        traceback.print_exc()
        return False
  
def get_today_ibkr_executions_and_positions(client_id=102):
    ib = IB()
    ib.connect('127.0.0.1', 4002, clientId=client_id, timeout=5)

    # Get today's executions
    executions = ib.reqExecutions()
    exec_data = []
    local_tz = pytz.timezone("America/Toronto")
    today = datetime.now(local_tz).date()
    
    for ex in executions:
        exec_time = ex.exec.time.astimezone(local_tz)
        if exec_time.date() == today:
            exec_data.append({
                "symbol": ex.contract.symbol,
                "side": ex.exec.side,
                "shares": ex.exec.shares,
                "price": ex.exec.price,
                "time": exec_time.isoformat(),
                "orderId": ex.exec.orderId
            })
    exec_df = pd.DataFrame(exec_data)

    # Get current positions
    positions = ib.positions()
    pos_data = [{
        "symbol": pos.contract.symbol,
        "position": pos.position,
        "avgCost": pos.avgCost,
        "account": pos.account
    } for pos in positions]
    pos_df = pd.DataFrame(pos_data)

    ib.disconnect()
    return exec_df, pos_df

  
def fetch_account_summary(ib):
    try:
        if not ib.isConnected():
            print("❌ IB not connected.")
            return None

        summary = ib.accountSummary()
        if not summary:
            print("⚠️ No account summary returned.")
            return None

        df = util.df(summary)
        print("✅ Account summary fetched (via accountSummary).")
        return df

    except Exception as e:
        print(f"❌ Failed to fetch IBKR account summary: {e}")
        return None

# === Order Placement ===
def place_ibkr_order(symbol: str, quantity: int, action: str = 'BUY', currency: str = 'USD'):
    
    df = fetch_account_summary(ib)
    if df is not None and not df.empty:
        print(df[["tag", "value", "currency"]])
    else:
        print("⚠️ Fallback account summary not available.")

    if not connect_ibkr():
        print("❌ Skipping order, IBKR connection not established.")
        return {
            "symbol": symbol,
            "qty": quantity,
            "action": action,
            "status": "failed",
            "error": "IBKR connection failed",
            "submitted_time": datetime.now(LOCAL_TZ).isoformat()
        }

    try:
        contract = Stock(symbol, 'SMART', currency)
        ib.qualifyContracts(contract)

        order = MarketOrder(action, quantity)
        trade = ib.placeOrder(contract, order)
        ib.sleep(1)  # Give IBKR time to assign order ID
        trades = ib.trades()
        print("📋 Current Trades Summary:")
        for t in trades:
            order_id = getattr(t.order, "orderId", None)
            symbol = getattr(t.contract, "symbol", None)
            status = getattr(t.orderStatus, "status", "Unknown")

            if order_id and order_id > 0 and symbol:
                print(f"📦 Status: {status} | Symbol: {symbol} | OrderID: {order_id}")

        print(f"🚀 Order placed: {action} {quantity}x {symbol} [OrderId: {trade.order.orderId}]")
        return {
            "symbol": symbol,
            "qty": quantity,
            "action": action,
            "status": "submitted",
            "orderId": trade.order.orderId,
            "submitted_time": datetime.now(LOCAL_TZ).isoformat()
        }
        
    except Exception as e:
        print(f"❌ Error placing IBKR order for {symbol}: {e}")
        return {
            "symbol": symbol,
            "qty": quantity,
            "action": action,
            "status": "failed",
            "error": str(e),
            "submitted_time": datetime.now(LOCAL_TZ).isoformat()
        }

# === Order Logging ===
def log_ibkr_order(order_data: dict):
    if not os.path.exists("exports"):
        os.makedirs("exports")

    try:
        if os.path.exists(IBKR_ORDER_LOG):
            with open(IBKR_ORDER_LOG, "r") as f:
                logs = json.load(f)
        else:
            logs = []

        order_data["submitted_time"] = datetime.now(LOCAL_TZ).isoformat()
        logs.append(order_data)
        atomic_save_json(logs, IBKR_ORDER_LOG)

        print(f"📝 Order logged: {order_data.get('action')} {order_data.get('symbol')} x{order_data.get('qty')}")
    except Exception as e:
        print(f"❌ Failed to log IBKR order: {e}")

# === IBKR Order Status and PnL ===
def get_ibkr_order_pnl(order_id):
    if not connect_ibkr():
        return None
    try:
        for t in ib.trades():
            if t.order.orderId == order_id:
                return {
                    "realized_pnl": round(t.orderStatus.realizedPNL or 0.0, 2),
                    "unrealized_pnl": round(t.pnl or 0.0, 2),
                    "fill_status": t.orderStatus.status or "Unknown"
                }
        return {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "fill_status": "NotFound"}
    except Exception as e:
        print(f"❌ Error fetching PnL for order {order_id}: {e}")
        return {
            "realized_pnl": None,
            "unrealized_pnl": None,
            "fill_status": f"error: {e}"
        }

def check_ibkr_order_status(order_id):
    if not connect_ibkr():
        return "disconnected"
    try:
        for order in ib.openOrders():
            if order.orderId == order_id:
                return order.orderStatus.status
        return "not_found"
    except Exception as e:
        return f"error: {e}"

# === Funds ===
def get_ibkr_available_funds():
    if not connect_ibkr():
        return 0.0
    try:
        account = ib.accountSummary()
        for row in account:
            if row.tag == "AvailableFunds" and row.currency == "USD":
                return float(row.value)
        return 0.0
    except Exception as e:
        print(f"❌ Failed to fetch IBKR available funds: {e}")
        return 0.0

# === Load Logs ===
def load_ibkr_orders_log():
    if not os.path.exists(IBKR_ORDER_LOG):
        return pd.DataFrame()
    try:
        with open(IBKR_ORDER_LOG, "r") as f:
            data = json.load(f)
        return pd.DataFrame(data)
    except Exception as e:
        print(f"❌ Error reading IBKR orders log: {e}")
        return pd.DataFrame()

# === Disconnect ===
def disconnect_ibkr():
    if ib.isConnected():
        ib.disconnect()
        print("🔌 IBKR disconnected.")
