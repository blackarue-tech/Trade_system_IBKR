# Wallet_Utils.py – Final Guarded Version with Lock + Summary

import os
import json
import pytz
from datetime import datetime

#STARTING_CAPITAL = 10000.0
MAX_DEPLOYABLE_CAPITAL = 10000.0
LOCK_FILE = "exports/wallet.lock"
SUMMARY_FILE = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/test_env/exports/wallet_summary.json")

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
    today = datetime.now(pytz.timezone("America/Toronto")).strftime("%Y-%m-%d")
    if today not in trades_data:
        print("⚠️ No trades found for today.")
        return 0.0

    trades = trades_data[today].get("trades", [])
    if not trades:
        return 0.0

    # Sort by entry time
    trades_sorted = sorted(trades, key=lambda t: t.get("datetime", ""))
    capital_total = sum(t.get("capital_used", 0.0) for t in trades_sorted if "capital_used" in t)

    print(f"📈 Inferred capital used at start of {today}: ${capital_total:.2f}")
    return capital_total

def audit_wallet(trades_file, wallet_file):
    with open(trades_file) as f:
        trades_data = json.load(f)
    pnl_total = 0
    ## The `initial_balance` variable in the `audit_wallet` function is being used to infer the
    # starting balance for the current day based on the trades data provided. It calls the
    # `infer_starting_balance_from_today` function to calculate the total capital used at the start
    # of the day by summing up the `capital_used` values from the trades data for the current day.
    # This inferred initial balance is then used in the calculation of the expected wallet balance
    # for auditing purposes.
    initial_balance = infer_starting_balance_from_today(trades_data)
    initial_balance = 10000
    capital_locked = 0
    for day_trades in trades_data.values():
        for trade in day_trades.get("trades", []):
            if trade.get("status") == "closed":
                pnl_total += trade.get("pnl", 0)
            elif trade.get("status") == "open":
                capital_locked += trade.get("capital_used", 0)

    expected_balance = initial_balance + pnl_total - capital_locked

    try:
        with open(wallet_file) as f:
            current_balance = json.load(f).get("balance", 0.0)
    except FileNotFoundError:
        current_balance = 0.0

    print("\n📊 WALLET AUDIT REPORT")
    print("-------------------------")
    print(f"Inferred starting balance today: ${initial_balance:.2f}")
    print(f"PnL from closed trades:           ${pnl_total:.2f}")
    print(f"Capital in open trades:           ${capital_locked:.2f}")
    print(f"📈 Expected wallet balance:       ${expected_balance:.2f}")
    print(f"📘 Actual wallet.json balance:    ${current_balance:.2f}")
    print(f"🔎 Drift:                         ${current_balance - expected_balance:.2f}\n")

if __name__ == "__main__":
    os.makedirs("exports", exist_ok=True)
    wallet_path = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/test_env/exports/wallet.json")
    trade_path = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/test_env/exports/live_trades.json")
    audit_wallet(trade_path, wallet_path)
