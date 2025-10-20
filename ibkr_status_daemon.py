# ibkr_status_daemon.py
import time, json, os
from datetime import datetime
from typing import Dict, Any
from ib_insync import IB, Trade

LIVE_TRADES_PATH = "exports/live_trades.json"

def load_json(p):
    return json.load(open(p, "r", encoding="utf-8")) if os.path.exists(p) else {}

def atomic_save_json(p, data):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)

def map_trade_status(ib_status: str) -> str:
    # You can extend this map if needed
    s = ib_status.lower()
    if s == "filled":
        return "filled"
    if s in ("submitted", "presubmitted"):
        return "working"
    if s in ("cancelled", "apicancelled", "inactive"):
        return "cancelled"
    return s

def apply_fill_updates(ib: IB, live_trades: Dict[str, Any]):
    # Pull all active orders
    trades: list[Trade] = ib.trades()
    # Positions could be used to confirm "open" state & avgCost
    positions = { (p.contract.symbol): p for p in ib.positions() }

    for day_key, bucket in live_trades.items():
        for t in bucket.get("trades", []):
            if t.get("broker") != "IBKR":
                continue

            # BUY side fills -> pending_buy -> open
            if t.get("status") == "pending_buy":
                orderId = t.get("broker_order", {}).get("orderId")
                mstatus = None
                avg_fill = None
                if orderId:
                    for tr in trades:
                        if getattr(tr.order, "orderId", None) == orderId:
                            mstatus = map_trade_status(tr.orderStatus.status)
                            avg_fill = tr.orderStatus.avgFillPrice or avg_fill
                if mstatus == "filled":
                    # Mark open & set entry price
                    t["status"] = "open"
                    if avg_fill:
                        t["entry_price"] = float(avg_fill)
                        t["entry_price_source"] = "ibkr_fill"
                    else:
                        # fallback: try position avg
                        pos = positions.get(t["symbol"])
                        if pos:
                            t["entry_price"] = float(pos.avgCost)  # IB gives total cost basis; close enough for now
                            t["entry_price_source"] = "ibkr_position"

            # SELL side fills -> pending_sell -> closed
            elif t.get("status") == "pending_sell":
                orderId = t.get("broker_order_sell", {}).get("orderId")
                mstatus = None
                avg_fill = None
                if orderId:
                    for tr in trades:
                        if getattr(tr.order, "orderId", None) == orderId:
                            mstatus = map_trade_status(tr.orderStatus.status)
                            avg_fill = tr.orderStatus.avgFillPrice or avg_fill
                if mstatus == "filled":
                    exit_price = float(avg_fill) if avg_fill else None
                    t["exit_time"] = datetime.now().isoformat()
                    if exit_price is not None:
                        t["exit_price"] = exit_price
                        ep = t.get("entry_price") or 0.0
                        sh = int(t.get("shares", 0))
                        t["pnl"] = round((exit_price - ep) * sh, 2)
                        t["pnl_pct"] = ((exit_price - ep) / ep) * 100 if ep else 0.0
                    t["status"] = "closed"

def main():
    ib = IB()
    # Adjust host/port/clientId as per your setup
    ib.connect('127.0.0.1', 4002, clientId=29)

    try:
        while True:
            live_trades = load_json(LIVE_TRADES_PATH)
            apply_fill_updates(ib, live_trades)
            atomic_save_json(LIVE_TRADES_PATH, live_trades)
            ib.sleep(1.0)
            time.sleep(2.0)
    finally:
        ib.disconnect()

if __name__ == "__main__":
    main()
