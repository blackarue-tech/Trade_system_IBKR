# Cancel all open orders for our client IDs (BUY/SELL/SYNC)
# Usage:  python cancel_all_ibkr.py [host] [port] [client_ids...]
# Example: python cancel_all_ibkr.py 127.0.0.1 7497 1019 1020 1021

import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 7497
CLIENTS = [int(x) for x in sys.argv[3:]] if len(sys.argv) > 3 else [1019, 1020, 1021]

def cancel_for_client(cid: int):
    from ib_insync import IB
    ib = IB()
    try:
        ib.connect(HOST, PORT, clientId=cid)
        trades = ib.trades()
        count = 0
        for tr in trades:
            st = (tr.orderStatus.status or "").lower()
            if st in ("presubmitted","submitted","pendingsubmit","pendingcancel","inactive"):
                ib.cancelOrder(tr.order)
                count += 1
        ib.sleep(0.2)
        print(f"[Client {cid}] Cancelled {count} working orders.")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    for c in CLIENTS:
        try:
            cancel_for_client(c)
        except Exception as e:
            print(f"[Client {c}] Error: {e}")
