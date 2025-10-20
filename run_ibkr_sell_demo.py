# run_ibkr_sell_demo.py
# Usage:
#   python run_ibkr_sell_demo.py AAPL           -> sell entire AAPL position
#   python run_ibkr_sell_demo.py AAPL 2         -> sell 2 shares of AAPL

import sys, time, json, os
from datetime import datetime
from brokers.ibkr_client import IbkrClient
from brokers.ibkr_orders import place_market_sell, place_market_sell_all
from brokers.ibkr_state import load_state
from core.reconcile import reconcile_once

def _get_position_qty(ib, symbol: str) -> int:
    """Return current long position size for symbol (0 if none)."""
    try:
        for p in ib.positions():
            if getattr(p.contract, "symbol", None) == symbol:
                return int(round(p.position))
    except Exception:
        pass
    return 0

def main():
    symbol = sys.argv[1] if len(sys.argv) >= 2 else "AAPL"
    qty = int(sys.argv[2]) if len(sys.argv) >= 3 else None  # None => sell all

    # One persistent client for orders
    orders_client = IbkrClient("orders")
    assert orders_client.ping(), "IBKR not healthy"
    ib = orders_client.ensure()

    # Show current position
    pos_qty = _get_position_qty(ib, symbol)
    print(f"📊 Current position {symbol}: {pos_qty} shares")

    if pos_qty <= 0:
        print(f"❌ No position to sell for {symbol}. Exiting.")
        return

    # Place SELL
    if qty is None or qty >= pos_qty:
        resp = place_market_sell_all(symbol, client=orders_client)
    else:
        resp = place_market_sell(symbol, qty=qty, client=orders_client)
    print("🧾 Sell response:", resp)

    # Watch fills via the status daemon snapshot and reconcile into live_trades.json
    for i in range(8):
        time.sleep(2)
        try:
            reconcile_once()
        except Exception as e:
            print(f"reconcile_once error: {e}")
        state = load_state()
        fills_tail = state.get("fills", [])[-3:]
        print(f"📸 [{i+1}] fills_tail:", fills_tail)

    # Final position check
    ib = orders_client.ensure()
    pos_qty_after = _get_position_qty(ib, symbol)
    print(f"✅ Final position {symbol}: {pos_qty_after} shares")

if __name__ == "__main__":
    main()
