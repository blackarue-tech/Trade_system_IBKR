import os, json, time
from datetime import datetime
from ib_insync import Trade
from brokers.ibkr_client import IbkrClient
from brokers.ibkr_state import load_state, save_state

STATE_PATH = os.path.join("exports", "ibkr_state.json")

POLL_SECS = 3  # 2–5 seconds is fine

def collect_snapshot():
    client = IbkrClient(role="status_daemon")
    ib = client.ensure()

    # Active trades (orders)
    open_trades = list(ib.trades())

    # Positions
    positions = ib.positions()  # returns Position objects

    # Executions (recent fills)
    # We can rely on trade.log from Trade objects; here keep it simple.

    state = {
        "timestamp": datetime.now().isoformat(),
        "orders": [],
        "positions": []
    }

    # Orders
    for t in open_trades:
        # t: Trade
        state["orders"].append({
            "orderId": t.order.orderId,
            "symbol": t.contract.symbol,
            "action": t.order.action,
            "qty": t.order.totalQuantity,
            "status": t.orderStatus.status,  # Submitted, Filled, PartiallyFilled, Cancelled, Inactive
            "filled": t.orderStatus.filled,
            "avgFillPrice": t.orderStatus.avgFillPrice,
            "remaining": t.orderStatus.remaining
        })

    # Positions
    for p in positions:
        state["positions"].append({
            "symbol": p.contract.symbol,
            "position": p.position,
            "avgCost": p.avgCost
        })

    return state

def run_loop():
    while True:
        try:
            state = collect_snapshot()
            save_state(state)
        except Exception as e:
            # Soft log: you can add file logging if desired
            pass
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    print("IBKR Status Daemon started. Collecting snapshots every {} seconds...".format(POLL_SECS))
    run_loop()
    
