# Market-close positions for symbols in exports/live_trades.json (paper testing helper)
# Usage: python flatten_positions.py [root=. ] [host] [port] [client_id]
# WARNING: This will place MARKET orders to close positions. Use on paper!

import json, sys
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
HOST = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 7497
CLIENT = int(sys.argv[4]) if len(sys.argv) > 4 else 1022  # separate client

exports = root / "exports"
lt_path = exports / "live_trades.json"
symbols = set()

try:
    data = json.loads(lt_path.read_text(encoding="utf-8"))
    for _, bucket in (data or {}).items():
        for t in bucket.get("trades", []):
            s = t.get("symbol")
            if s: symbols.add(s)
except Exception:
    pass

from ib_insync import IB, Stock, MarketOrder

ib = IB()
try:
    ib.connect(HOST, PORT, clientId=CLIENT)
    # Map current positions
    pos = {p.contract.symbol: p for p in ib.positions()}
    closed = 0
    for sym in symbols:
        p = pos.get(sym)
        if not p:
            continue
        qty = int(getattr(p, "position", 0))
        if qty == 0:
            continue
        action = "SELL" if qty > 0 else "BUY"
        contract = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(contract)
        o = MarketOrder(action, abs(qty))
        tr = ib.placeOrder(contract, o)
        print(f"Flatten {sym}: {action} {abs(qty)} MKT (pos={qty}) -> orderId={getattr(tr.order,'orderId',None)}")
        closed += 1
    print(f"Flattened {closed} symbols.")
finally:
    try:
        ib.disconnect()
    except Exception:
        pass
