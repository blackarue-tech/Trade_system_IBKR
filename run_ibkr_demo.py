# run_ibkr_demo.py
import os, json, time
from brokers.ibkr_client import IbkrClient
from brokers.ibkr_orders import place_market_buy, place_market_sell_all
from brokers.ibkr_state import load_state
from core.reconcile import reconcile_once

def _j(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

LIVE_PARAMS = os.path.join("configs", "live_params.json")

def demo_buy(symbol: str, last_price: float, context_overrides: dict | None = None):
    live_params = _j(LIVE_PARAMS)
    ctx = {
        "confidence": 0.62,
        "live_params": live_params,
        "ibkr_balance_usd": 50000.0,
        "open_symbols": [],
        "above_vwap": True,
        "ma_fast_gt_ma_slow": True,
        "neg_momentum": False,
        "recent_pct_change": 0.5
    }
    if context_overrides:
        ctx.update(context_overrides)

    # SINGLE orders client for the whole run
    orders_client = IbkrClient("orders")
    assert orders_client.ping(), "IBKR not healthy"

    # Place guarded BUY using the same client
    #resp = place_market_buy(symbol, last_price, ctx, client=orders_client)
    #print("Order response:", resp)

    # SELL entire position
    resp = place_market_sell_all("AAPL", client=orders_client)
    print("Sell response:", resp)
    
    for _ in range(6):
        time.sleep(3)
        reconcile_once()
        state = load_state()
        print("Fills (latest 3):", state.get("fills", [])[-3:])


if __name__ == "__main__":
    print("Starting IBKR demo...")
    #demo_buy("AAPL", 225.00, {})
