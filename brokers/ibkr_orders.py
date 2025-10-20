from ib_insync import Stock, MarketOrder, LimitOrder
import json, os, time
from datetime import datetime
from .ibkr_client import IbkrClient, get_client
from core.market_hours import market_is_open
from core.sizing import compute_quantity
from core.guards import should_buy_symbol
from utils import atomic_append_json

CONFIG_ROUTING = os.path.join("configs", "routing.json")
ORDERS_LOG = os.path.join("exports", "ibkr_orders_log.json")

def _load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def _get_position_qty(ib, symbol: str) -> int:
    """Return current long position size for symbol (0 if none)."""
    try:
        for p in ib.positions():
            if getattr(p.contract, "symbol", None) == symbol:
                # IB returns float, round down to int shares
                q = int(round(p.position))
                return max(q, 0)
    except Exception:
        pass
    return 0

def place_market_buy_qty(
    symbol: str,
    qty: int,
    *,
    client: IbkrClient | None = None
) -> dict:
    
    if qty <= 0:
        return {"status": "blocked", "reason": "zero_qty"}

    client = client or IbkrClient(role="orders")
    ib = client.ensure()

    routing = _load_json(CONFIG_ROUTING)["equities"]
    contract = Stock(symbol, routing["exchange"], routing.get("currency", "USD"))
    ib.qualifyContracts(contract)

    order = MarketOrder("BUY", int(qty), tif=routing.get("tif", "DAY"))
    trade = ib.placeOrder(contract, order)

    record = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "action": "BUY",
        "order_type": "MKT",
        "qty": int(qty),
        "status": "submitted",
        "orderId": trade.order.orderId
    }
    atomic_append_json(ORDERS_LOG, record)
    return record

def place_market_buy(symbol: str, last_price: float, context: dict, *, client: IbkrClient | None = None) -> dict:
    """
    context: { 'confidence': float, 'live_params': {...}, 'ibkr_balance_usd': float, 'open_symbols': set([...]) }
    Reuses the provided IbkrClient to avoid duplicate clientId connections.
    """
    if not market_is_open():
        return {"status": "blocked", "reason": "market_closed"}

    if not should_buy_symbol(symbol, last_price, context):
        return {"status": "blocked", "reason": "guards_rejected"}

    routing = _load_json(CONFIG_ROUTING)["equities"]
    qty = compute_quantity(symbol, last_price, context)
    if qty <= 0:
        return {"status": "blocked", "reason": "zero_qty"}

    client = client or get_client("orders")
    ib = client.ensure()

    contract = Stock(symbol, routing["exchange"], routing.get("currency", "USD"))
    ib.qualifyContracts(contract)

    order = MarketOrder("BUY", qty, tif=routing.get("tif", "DAY"))
    trade = ib.placeOrder(contract, order)

    record = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "action": "BUY",
        "order_type": "MKT",
        "qty": qty,
        "intended_price": last_price,
        "status": "submitted",
        "orderId": trade.order.orderId
    }
    atomic_append_json(ORDERS_LOG, record)
    return record

def place_market_sell(
    symbol: str,
    qty: int | None = None,
    *,
    client: IbkrClient | None = None,
    close_all: bool = False
) -> dict:
    """
    Place a market SELL for `symbol`.
    - If `qty` is None or `close_all=True`, sell the entire current position.
    - Reuses the provided `client` to avoid clientId conflicts.
    """
    if not market_is_open():
        return {"status": "blocked", "reason": "market_closed"}

    client = client or IbkrClient(role="orders")
    ib = client.ensure()

    routing = _load_json(CONFIG_ROUTING)["equities"]

    pos_qty = _get_position_qty(ib, symbol)
    if pos_qty <= 0:
        return {"status": "blocked", "reason": "no_position"}

    sell_qty = pos_qty if (close_all or qty is None) else min(int(qty), pos_qty)
    if sell_qty <= 0:
        return {"status": "blocked", "reason": "zero_qty"}

    contract = Stock(symbol, routing["exchange"], routing.get("currency", "USD"))
    ib.qualifyContracts(contract)

    order = MarketOrder("SELL", sell_qty, tif=routing.get("tif", "DAY"))
    trade = ib.placeOrder(contract, order)

    record = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "action": "SELL",
        "order_type": "MKT",
        "qty": sell_qty,
        "status": "submitted",
        "orderId": trade.order.orderId
    }
    atomic_append_json(ORDERS_LOG, record)
    return record

def place_market_sell_all(symbol: str, *, client: IbkrClient | None = None) -> dict:
    """Convenience wrapper to close the entire position."""
    return place_market_sell(symbol, qty=None, client=client, close_all=True)
