# modules/broker_ibkr.py
from datetime import datetime
from typing import Dict, Any

try:
    from ib_insync import IB, Stock, MarketOrder
except Exception:
    IB = None

_IB: IB | None = None

def _ib() -> IB | None:
    global _IB
    if IB is None:
        return None
    if _IB is None:
        _IB = IB()
        try:
            # Default paper trading gateway; adjust host/port/clientId as you already use elsewhere
            _IB.connect('127.0.0.1', 7497, clientId=19)
        except Exception:
            return None
    return _IB

def place_market_order(symbol: str, quantity: int, action: str) -> Dict[str, Any]:
    """Place a simple market order. Returns a small, engine-friendly dict."""
    ib = _ib()
    if ib is None:
        return {"ok": False, "status": "failed", "orderId": None, "error": "IBKR not connected"}

    try:
        contract = Stock(symbol, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        order = MarketOrder(action.upper(), int(quantity))
        trade = ib.placeOrder(contract, order)
        # trade has live lifecycle; we only return what the engine needs immediately
        return {
            "ok": True,
            "status": "submitted",
            "orderId": getattr(trade.order, "orderId", None),
            "error": None,
            "submitted_time": datetime.now().isoformat()
        }
    except Exception as e:
        return {"ok": False, "status": "failed", "orderId": None, "error": str(e)}
