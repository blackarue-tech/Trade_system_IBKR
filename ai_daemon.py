# ai_daemon.py - Advanced Suspension/Recovery AI Daemon

import os
import json
import time
from datetime import datetime
from pytz import timezone

# === CONFIG ===
LOCAL_TZ = timezone("America/Toronto")
TRADES_PATH = "exports/live_trades.json"
CONTROL_PATH = "inputs/ai_strategy_configs/control_params.json"
TRACKER_PATH = "exports/symbol_tracker.json"
TUNE_INTERVAL = 60  # seconds
PNL_SUSPEND_THRESHOLD = -3
PNL_REACTIVATE_THRESHOLD = 3
RECOVERY_SIMULATIONS_REQUIRED = 3

# === UTILITIES ===
def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_json(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def compute_symbol_pnl(trades, tracker):
    symbol_pnl = {}
    for day, data in trades.items():
        for trade in data.get("trades", []):
            symbol = trade.get("symbol")
            if symbol is None:
                continue
            if symbol in tracker.get("suspended", {}):
                continue
            reactivation_ts = tracker.get("reactivated", {}).get(symbol)
            if reactivation_ts:
                trade_dt = datetime.fromisoformat(trade.get("datetime"))
                react_dt = datetime.fromisoformat(reactivation_ts)
                if trade_dt < react_dt:
                    continue  # ignore trades before reactivation
            if trade.get("status") == "closed":
                pnl = trade.get("pnl", 0)
                symbol_pnl[symbol] = symbol_pnl.get(symbol, 0) + pnl
    return symbol_pnl

# === MAIN LOGIC ===
def tune_symbols():
    trades = load_json(TRADES_PATH)
    control = load_json(CONTROL_PATH)
    tracker = load_json(TRACKER_PATH)

    suspended = set(control.get("suspended_symbols", []))
    prioritized = set(control.get("prioritized_symbols", []))

    tracker.setdefault("suspended", {})
    tracker.setdefault("reactivated", {})
    tracker.setdefault("recovery_counter", {})

    symbol_pnl = compute_symbol_pnl(trades, tracker)

    for symbol, pnl in symbol_pnl.items():
        # If already suspended
        if symbol in suspended:
            if pnl >= PNL_REACTIVATE_THRESHOLD:
                tracker["recovery_counter"][symbol] = tracker["recovery_counter"].get(symbol, 0) + 1
                print(f"🩺 {symbol} recovery progress: {tracker['recovery_counter'][symbol]}/{RECOVERY_SIMULATIONS_REQUIRED}")
                if tracker["recovery_counter"][symbol] >= RECOVERY_SIMULATIONS_REQUIRED:
                    suspended.remove(symbol)
                    tracker["reactivated"][symbol] = datetime.now(LOCAL_TZ).isoformat()
                    tracker["suspended"].pop(symbol, None)
                    tracker["recovery_counter"].pop(symbol, None)
                    print(f"✅ {symbol} reactivated for trading.")
            else:
                tracker["recovery_counter"][symbol] = 0  # reset recovery counter if test fails
        else:
            if pnl <= PNL_SUSPEND_THRESHOLD:
                suspended.add(symbol)
                tracker["suspended"][symbol] = datetime.now(LOCAL_TZ).isoformat()
                tracker["reactivated"].pop(symbol, None)
                tracker["recovery_counter"][symbol] = 0
                prioritized.discard(symbol)
                print(f"🚫 {symbol} suspended due to consistent low PnL.")
            elif pnl >= PNL_REACTIVATE_THRESHOLD:
                prioritized.add(symbol)

    updated_control = {
        "suspended_symbols": sorted(list(suspended)),
        "prioritized_symbols": sorted(list(prioritized))
    }

    save_json(updated_control, CONTROL_PATH)
    save_json(tracker, TRACKER_PATH)

    now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{now_str}] 🤖 AI Daemon updated symbol control:")
    print(json.dumps(updated_control, indent=2))

# === LOOP ===
def run_loop():
    while True:
        tune_symbols()
        print(f"[{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] ⏱️ Sleeping {TUNE_INTERVAL}s before next tuning cycle.")
        time.sleep(TUNE_INTERVAL)

if __name__ == "__main__":
    run_loop()
