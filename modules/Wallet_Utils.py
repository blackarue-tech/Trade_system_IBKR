# Wallet_Utils.py – Final Guarded Version with Lock + Summary

import os
import json
import pytz
from datetime import datetime
from pytz import timezone

#STARTING_CAPITAL = 10000.0
MAX_DEPLOYABLE_CAPITAL = 10000.0
LOCK_FILE = "exports/wallet.lock"
WALLET_PATH = "exports/wallet.json"
TRADE_LOG_PATH = "exports/live_trades.json"
SUMMARY_FILE = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/test_env/exports/wallet_summary.json")
LOCAL_TZ = timezone("America/Toronto")

def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_json(data, path):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# === Load/Save Wallet Balance ===
def load_wallet_balance(wallet_path):
    if not os.path.exists(wallet_path):
        raise FileNotFoundError(f"❌ Wallet file not found at {wallet_path}. Aborting to prevent capital reset.")        
    #print(f"🧾 Wallet Failed")
    with open(wallet_path, "r") as f:
        return json.load(f).get("balance", 0.0)

def save_wallet_balance(wallet_path, balance):
    with open(wallet_path, "w") as f:
        json.dump({"balance": round(balance, 2)}, f, indent=2)

# === Capital Access Control ===
def get_wallet_available_capital(wallet_path):
    balance = load_wallet_balance(wallet_path)
    return min(balance, MAX_DEPLOYABLE_CAPITAL)

def debit_wallet_balance(wallet_path, amount):
    balance = load_wallet_balance(wallet_path)
    if balance >= amount:
        new_balance = balance - amount
        save_wallet_balance(wallet_path, new_balance)
        print(f"💸 Debited ${amount:.2f} from wallet. New balance: ${new_balance:.2f}")
        return True
    else:
        print(f"❌ Insufficient funds. Wallet has ${balance:.2f}, tried to debit ${amount:.2f}")
        return False

def credit_wallet_balance(wallet_path, amount):
    balance = load_wallet_balance(wallet_path)
    new_balance = balance + amount
    save_wallet_balance(wallet_path, new_balance)
    print(f"💰 Credited ${amount:.2f} to wallet. New balance: ${new_balance:.2f}")

# === Wallet Lock Guard ===
def is_wallet_locked():
    return os.path.exists(LOCK_FILE)

def lock_wallet():
    with open(LOCK_FILE, "w") as f:
        f.write(datetime.now().isoformat())

def reset_wallet_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

# === Summary Logging ===
def log_wallet_summary(wallet_path, log_path=SUMMARY_FILE):
    today = datetime.now().strftime("%Y-%m-%d")
    balance = load_wallet_balance(wallet_path)
    log_data = {}
    if os.path.exists(log_path):
        with open(log_path) as f:
            log_data = json.load(f)
    log_data[today] = {"balance": balance}
    with open(log_path, "w") as f:
        json.dump(log_data, f, indent=2)

# === Audit Tool ===
def infer_starting_balance_from_today(trades_data):
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    if today in trades_data:
        day_trades = trades_data[today].get("trades", [])
        if day_trades:
            first_trade = day_trades[0]
            return first_trade.get("wallet_balance_at_entry", 10000.0)
    return 10000.0  # fallback if no data

def audit_wallet():
    wallet_data = load_json(WALLET_PATH)
    current_balance = wallet_data.get("balance", 10000.0) if isinstance(wallet_data, dict) else wallet_data
    trades = load_json(TRADE_LOG_PATH)

    initial_balance = infer_starting_balance_from_today(trades)
    realized_pnl = 0
    locked_capital = 0

    for day, data in trades.items():
        for trade in data.get("trades", []):
            if trade.get("status") == "closed":
                realized_pnl += trade.get("pnl", 0)
            elif trade.get("status") == "open":
                locked_capital += trade.get("capital_used", 0)

    expected_balance = initial_balance + realized_pnl
    drift = expected_balance - current_balance

    now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{now_str}] 🧮 WALLET AUDIT")
    print(f"Inferred Starting Balance: ${initial_balance:.2f}")
    print(f"Realized PnL: ${realized_pnl:.2f}")
    print(f"Expected Balance: ${expected_balance:.2f}")
    print(f"Current Wallet Balance: ${current_balance:.2f}")
    print(f"Locked Capital (Open Trades): ${locked_capital:.2f}")
    print(f"Calculated Drift: ${drift:.2f}")

    if abs(drift) > 0.01:
        print(f"✅ Wallet adjusted by ${drift:.2f} to preserve capital integrity.")
        save_json({"balance": round(expected_balance, 2)}, WALLET_PATH)
    else:
        print(f"✅ Wallet is consistent. No adjustment needed.")

if __name__ == "__main__":
    os.makedirs("exports", exist_ok=True)
    wallet_path = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/test_env/exports/wallet.json")
    trade_path = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/test_env/exports/live_trades.json")
    audit_wallet()
