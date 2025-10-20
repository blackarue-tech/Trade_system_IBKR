import math

def compute_quantity(symbol: str, last_price: float, ctx: dict) -> int:
    alloc = ctx["live_params"]["capital_allocation"]
    cap = float(alloc.get("per_trade_cap_usd", 1000))
    if last_price <= 0:
        return 0
    qty = math.floor(cap / last_price)
    # Hard floor to avoid dust
    return max(qty, 0)
