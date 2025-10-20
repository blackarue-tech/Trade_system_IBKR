# order_reconciler.py
import json, os
from datetime import datetime
from pytz import timezone
from ibkr_bridge import get_order_status, get_open_positions
from utils import atomic_save_json

LOCAL_TZ = timezone("America/Toronto")
LIVE_TRADES_PATH = "exports/live_trades.json"

def load_json(path):
    return json.load(open(path)) if os.path.exists(path) else {}

def save_json(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_save_json(path, data)

def reconcile_once():
    trades = load_json(LIVE_TRADES_PATH)
    open_positions = get_open_positions()

    changed = False
    for day, payload in trades.items():
        for tr in payload.get("trades", []):
            status = tr.get("status")
            oid = tr.get("orderId")

            # flip to OPEN when buy is filled / position visible
            if status == "pending_buy":
                if oid:
                    st = get_order_status(oid)
                    if st["status"].lower() in ("filled", "presubmitted", "submitted") and float(st.get("filled", 0)) >= tr.get("shares", 0):
                        tr["status"] = "open"
                        tr["entry_price_executed"] = float(st.get("avgFillPrice") or tr.get("entry_price"))
                        changed = True
                # fallback: if position exists with expected shares
                pos = open_positions.get(tr["symbol"])
                if pos and pos["shares"] >= tr.get("shares", 0):
                    tr["status"] = "open"
                    tr["entry_price_executed"] = float(pos.get("avg_price") or tr.get("entry_price"))
                    changed = True

            # flip to CLOSED when sell filled / position cleared
            if status == "pending_sell":
                if oid:
                    st = get_order_status(oid)
                    if st["status"].lower() in ("filled",):
                        tr["status"] = "closed"
                        tr["exit_price"] = float(st.get("avgFillPrice") or tr.get("exit_price", tr.get("entry_price_executed", tr.get("entry_price"))))
                        tr["exit_time"] = datetime.now(LOCAL_TZ).isoformat()
                        # compute PnL
                        shares = tr.get("shares", 0)
                        entry = tr.get("entry_price_executed", tr.get("entry_price", 0.0))
                        tr["pnl"] = round((tr["exit_price"] - entry) * shares, 2)
                        tr["pnl_pct"] = round(((tr["exit_price"] - entry) / entry) * 100, 4) if entry else 0.0
                        changed = True
                # fallback: if no longer in open positions (flat)
                sym = tr["symbol"]
                pos = open_positions.get(sym)
                if not pos or pos["shares"] == 0:
                    if tr.get("exit_price") is None:
                        tr["exit_price"] = tr.get("exit_price") or tr.get("entry_price_executed", tr.get("entry_price"))
                    tr["status"] = "closed"
                    tr["exit_time"] = tr.get("exit_time") or datetime.now(LOCAL_TZ).isoformat()
                    shares = tr.get("shares", 0)
                    entry = tr.get("entry_price_executed", tr.get("entry_price", 0.0))
                    tr["pnl"] = round((tr["exit_price"] - entry) * shares, 2)
                    tr["pnl_pct"] = round(((tr["exit_price"] - entry) / entry) * 100, 4) if entry else 0.0
                    changed = True

    if changed:
        save_json(trades, LIVE_TRADES_PATH)

if __name__ == "__main__":
    import time
    while True:
        reconcile_once()
        time.sleep(3)
