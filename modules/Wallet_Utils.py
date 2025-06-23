# Wallet_Utils.py – Final Guarded Version with Lock + Summary

import os
import json
from datetime import datetime

STARTING_CAPITAL = 10000.0
MAX_DEPLOYABLE_CAPITAL = 10000.0
LOCK_FILE = "exports/wallet.lock"
SUMMARY_FILE = "exports/wallet_summary.json"

# === Load/Save Wallet Balance ===
def load_wallet_balance(wallet_path):
    if not os.path.exists(wallet_path):
        with open(wallet_path, "w") as f:
            json.dump({"balance": round(STARTING_CAPITAL, 2)}, f, indent=2)
        print(f"🧾 Wallet initialized with ${STARTING_CAPITAL:.2f}")
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
def audit_wallet(trades_file, wallet_file):
    with open(wallet_file) as f:
        balance = json.load(f).get("balance", 0.0)
    with open(trades_file) as f:
        data = json.load(f)

    pnl_total = 0
    for day_trades in data.values():
        for trade in day_trades.get("trades", []):
            if trade.get("status") == "closed":
                pnl_total += trade.get("pnl", 0)
            elif trade.get("status") == "open":
                pnl_total -= trade.get("capital_used", 0)

    expected_balance = STARTING_CAPITAL + pnl_total
    print("\n📊 WALLET AUDIT REPORT")
    print("-------------------------")
    print(f"Current wallet balance: ${balance:.2f}")
    print(f"Expected balance based on trades: ${expected_balance:.2f}")
    print(f"🔎 Drift: ${balance - expected_balance:.2f}\n")

if __name__ == "__main__":
    audit_wallet("exports/live_trades.json", "exports/wallet.json")
