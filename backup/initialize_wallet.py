# initialize_wallet.py
import json
import os

wallet_path = "exports/wallet.json"
initial_balance = 10000.00

if os.path.exists(wallet_path):
    confirm = input("⚠️ Wallet file already exists. Overwrite? (yes/no): ").lower()
    if confirm != "yes":
        print("🚫 Initialization aborted.")
        exit()

with open(wallet_path, "w") as f:
    json.dump({"balance": round(initial_balance, 2)}, f, indent=2)

print(f"✅ Wallet initialized with ${initial_balance:.2f}")
