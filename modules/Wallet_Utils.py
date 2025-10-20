# Wallet_Utils.py – Structured logging + fail-safe version

import os
import json
import pytz
from datetime import datetime
from pytz import timezone
import math

from modules.log_utils import log, fail_safe

MAX_DEPLOYABLE_CAPITAL = 10000.0
LOCK_FILE = "exports/wallet.lock"
WALLET_PATH = "exports/wallet.json"
TRADE_LOG_PATH = "exports/live_trades.json"
SUMMARY_FILE = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/test_env/exports/wallet_summary.json")
LOCAL_TZ = timezone("America/Toronto")

def load_json(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        log(f"Error loading JSON from {path}: {e}", "ERROR", "WalletUtils")
    return {}

def save_json(data, path):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Error saving JSON to {path}: {e}", "ERROR", "WalletUtils")

# === Load/Save Wallet Balance ===
@fail_safe(module="WalletUtils")
def load_wallet_balance(wallet_path):
    if not os.path.exists(wallet_path):
        raise FileNotFoundError(f"Wallet file not found at {wallet_path}. Aborting to prevent capital reset.")
    with open(wallet_path, "r") as f:
        balance = json.load(f).get("balance", 0.0)
    log(f"Wallet balance loaded: ${balance:.2f}", "INFO", "WalletUtils")
    return balance

@fail_safe(module="WalletUtils")
def save_wallet_balance(wallet_path, balance):
    save_json({"balance": round(balance, 2)}, wallet_path)
    log(f"Wallet balance saved: ${balance:.2f}", "INFO", "WalletUtils")

# === Capital Access Control ===
def get_wallet_available_capital(wallet_path):
    balance = load_wallet_balance(wallet_path)
    return min(balance, MAX_DEPLOYABLE_CAPITAL)

@fail_safe(module="WalletUtils")
def debit_wallet_balance(wallet_path, amount):
    balance = load_wallet_balance(wallet_path)
    if balance >= amount:
        new_balance = balance - amount
        save_wallet_balance(wallet_path, new_balance)
        log(f"Debited ${amount:.2f} from wallet. New balance: ${new_balance:.2f}", "INFO", "WalletUtils")
        return True
    else:
        log(f"Insufficient funds. Wallet has ${balance:.2f}, tried to debit ${amount:.2f}", "WARNING", "WalletUtils")
        return False

@fail_safe(module="WalletUtils")
def credit_wallet_balance(wallet_path, amount):
    balance = load_wallet_balance(wallet_path)
    new_balance = balance + amount
    save_wallet_balance(wallet_path, new_balance)
    log(f"Credited ${amount:.2f} to wallet. New balance: ${new_balance:.2f}", "INFO", "WalletUtils")

# === Wallet Lock Guard ===
def is_wallet_locked():
    return os.path.exists(LOCK_FILE)

def lock_wallet():
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(datetime.now().isoformat())
        log("Wallet locked.", "INFO", "WalletUtils")
    except Exception as e:
        log(f"Error locking wallet: {e}", "ERROR", "WalletUtils")

def reset_wallet_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        log("Wallet lock reset.", "INFO", "WalletUtils")

# === Summary Logging ===
@fail_safe(module="WalletUtils")
def log_wallet_summary(wallet_path, log_path=SUMMARY_FILE):
    today = datetime.now().strftime("%Y-%m-%d")
    balance = load_wallet_balance(wallet_path)
    log_data = {}
    if os.path.exists(log_path):
        with open(log_path) as f:
            log_data = json.load(f)
    log_data[today] = {"balance": balance}
    save_json(log_data, log_path)
    log(f"Wallet summary logged for {today}.", "INFO", "WalletUtils")

# === Audit Tool ===
def infer_starting_balance_from_today(trades_data):
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    if today in trades_data:
        day_trades = trades_data[today].get("trades", [])
        if day_trades:
            first_trade = day_trades[0]
            return first_trade.get("wallet_balance_at_entry", 10000.0)
    return 10000.0

@fail_safe(module="WalletUtils")
def audit_wallet():
    wallet_data = load_json(WALLET_PATH)
    current_balance = wallet_data.get("balance", 10000.0) if isinstance(wallet_data, dict) else wallet_data
    trades = load_json(TRADE_LOG_PATH)

    initial_balance = infer_starting_balance_from_today(trades)
    if isinstance(initial_balance, float) and math.isnan(initial_balance):
        initial_balance = 10000.0
    realized_pnl = 0
    locked_capital = 0
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
    log(f"[{now_str}] WALLET AUDIT", "INFO", "WalletUtils")
    log(f"Inferred Starting Balance: ${initial_balance:.2f}", "INFO", "WalletUtils")
    log(f"Realized PnL: ${realized_pnl:.2f}", "INFO", "WalletUtils")
    log(f"Expected Balance: ${expected_balance:.2f}", "INFO", "WalletUtils")
    log(f"Current Wallet Balance: ${current_balance:.2f}", "INFO", "WalletUtils")
    log(f"Locked Capital (Open Trades): ${locked_capital:.2f}", "INFO", "WalletUtils")
    log(f"Calculated Drift: ${drift:.2f}", "INFO", "WalletUtils")

    if abs(drift) > 0.01:
        log(f"Wallet adjusted by ${drift:.2f} to preserve capital integrity.", "INFO", "WalletUtils")
        save_json({"balance": round(expected_balance, 2)}, WALLET_PATH)
    else:
        log("Wallet is consistent. No adjustment needed.", "INFO", "WalletUtils")

if __name__ == "__main__":
    os.makedirs("exports", exist_ok=True)
    audit_wallet()
