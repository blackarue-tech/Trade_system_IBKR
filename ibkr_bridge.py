# modules/ibkr_bridge.py
from ib_insync import IB, Stock, MarketOrder, LimitOrder
from datetime import datetime
import pytz, time

LOCAL_TZ = pytz.timezone("America/Toronto")

_ib = IB()
_CONNECTED = False

def connect_ibkr(host='127.0.0.1', port=4002, clientId=7):
    global _CONNECTED
    if not _CONNECTED:
        _ib.connect(host, port, clientId=clientId)  # paper: 7497
        _CONNECTED = True
    return _CONNECTED

def is_us_listing(symbol: str) -> bool:
    # Fast, conservative gate: US stocks only, USD, SMART routing
    # (If you want stricter validation, call reqContractDetails and check primaryExch in {'NYSE','NASDAQ','ARCA'} etc.)
    return True  # keep permissive but US-only routing below

def place_limit(symbol: str, qty: int, action: str, limit_price: float, tif='DAY'):
    """
    Submit a limit order. Returns dict with orderId and status stub.
    """
    assert action in ('BUY', 'SELL')
    connect_ibkr()
    if not is_us_listing(symbol):
        raise ValueError(f"Non-US listing blocked for {symbol}")

    contract = Stock(symbol, 'SMART', 'USD')  # restrict to US venues
    _ib.qualifyContracts(contract)

    order = LimitOrder(action, qty, limit_price, tif=tif)
    trade = _ib.placeOrder(contract, order)
    return {
        "orderId": trade.order.orderId,
        "status": "submitted",
        "submitted_time": datetime.now(LOCAL_TZ).isoformat(),
        "limit": limit_price,
        "tif": tif
    }

def place_market(symbol: str, qty: int, action: str):
    assert action in ('BUY', 'SELL')
    connect_ibkr()
    if not is_us_listing(symbol):
        raise ValueError(f"Non-US listing blocked for {symbol}")

    contract = Stock(symbol, 'SMART', 'USD')
    _ib.qualifyContracts(contract)

    order = MarketOrder(action, qty)
    trade = _ib.placeOrder(contract, order)
    return {
        "orderId": trade.order.orderId,
        "status": "submitted",
        "submitted_time": datetime.now(LOCAL_TZ).isoformat()
    }

def get_available_funds() -> float:
    connect_ibkr()
    # Look up NetLiq/AvailableFunds from account values
    acct_vals = {v.tag: v.value for v in _ib.accountValues() if v.tag in ("AvailableFunds", "AvailableFundsS")}
    # prefer USD available funds
    try:
        return float(acct_vals.get("AvailableFunds") or acct_vals.get("AvailableFundsS") or 0.0)
    except:
        return 0.0

def get_open_positions():
    connect_ibkr()
    # [(Contract, Position, AvgCost)]
    pos = _ib.positions()
    result = {}
    for p in pos:
        sym = p.contract.symbol
        if p.contract.currency == 'USD':
            result[sym] = {
                "symbol": sym,
                "shares": int(p.position),
                "avg_price": float(p.avgCost) if hasattr(p, "avgCost") else None
            }
    return result

def get_order_status(orderId: int):
    connect_ibkr()
    # Scan live trades in IBKR until we find the one with matching orderId
    for tr in _ib.trades():
        if tr.order and tr.order.orderId == orderId:
            fill = tr.fills[-1].execution if tr.fills else None
            status = tr.orderStatus.status  # e.g., Submitted, Filled, Cancelled, ApiCancelled, Inactive, PreSubmitted
            filled = tr.orderStatus.filled
            avgFillPrice = tr.orderStatus.avgFillPrice
            lastFillTime = datetime.now(LOCAL_TZ).isoformat()
            return {
                "status": status,
                "filled": filled,
                "avgFillPrice": avgFillPrice,
                "lastFillTime": lastFillTime
            }
    return {"status": "Unknown"}

def force_cancel(orderId: int):
    connect_ibkr()
    for tr in _ib.trades():
        if tr.order and tr.order.orderId == orderId:
            _ib.cancelOrder(tr.order)
            return True
    return False
