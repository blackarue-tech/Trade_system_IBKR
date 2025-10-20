# backtest_engine.py — Polygon-first price of record, IBKR execution, NaN guards,
# pending unclogger, and 30-minute negative-PnL guard.
#
# BUY rule  : intended_entry >= market -> BUY @ MKT; else skip
# SELL rule : intended_exit  <= market -> SELL @ MKT; else wait, but force SELL @ MKT after 30 min
#
# Key points:
# - Prices: prefer fresh Polygon (<= polygon_max_delay_sec), fallback to IBKR snapshot
# - live_prices.json: flat map { "SYMBOL": last_price } (finite numbers only)
# - Robust handling of NaN/inf in prices and shares
# - IBKR pending unclog: replace to MKT, or marketable LIMIT fallback if needed
# - Separate client IDs for BUY / SELL / SYNC
# - Wallet removed

import os
import json
import time
import math
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, time as dtime, timezone as dtimezone

import pandas as pd
from pytz import timezone as pytz_timezone
import holidays

from modules.log_utils import log, fail_safe
from utils import atomic_save_json
# Optional imports retained for compatibility with your stack
from modules.model_utils import add_technical_indicators  # noqa: F401
from modules.ai_signals_pro import get_strategy_signals
from modules.price_data import get_price_data  # noqa: F401
from polygon_utils import (
    get_top_gainers_polygon,
    filter_gainers_for_momentum,
    get_polygon_quote, record_quote_delay
)

# ===== Paths & Config =====
LOCAL_TZ = pytz_timezone("America/Toronto")
us_holidays = holidays.US()

LIVE_TRADES_PATH = "exports/live_trades.json"
BUY_ORDERS_PATH = "exports/orders_buy.json"
SELL_ORDERS_PATH = "exports/orders_sell.json"
CONTROL_PATH = "inputs/ai_strategy_configs/control_params.json"
LIVE_PARAMS_PATH = "inputs/ai_strategy_configs/live_params.json"
EXIT_PARAMS_PATH = "inputs/ai_strategy_configs/exit_params.json"
LIVE_PRICES_PATH = "exports/live_prices.json"

CAPITAL_LIMIT = 10000
LOOP_INTERVAL = 30
MIN_CONFIDENCE = 0.5

# Policy (overridable via LIVE_PARAMS)
NEGATIVE_EXIT_BLOCK_SECS_DEFAULT = 1800     # 30 minutes
MAX_PENDING_TOTAL_DEFAULT = 6               # back-pressure for buys
POLYGON_MAX_DELAY_SEC_DEFAULT = 20.0        # prefer very fresh polygon
BUY_CLOG_CANCEL_SECS_DEFAULT = 180          # 3 minutes
MIN_BUY_SHARES_DEFAULT = 10                 # don’t buy tiny lots

# ===== IBKR connection =====
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4002"))       # TWS/IBG paper default
IB_CLIENT_ID_BUY  = int(os.getenv("IB_CLIENT_ID_BUY",  1019))
IB_CLIENT_ID_SELL = int(os.getenv("IB_CLIENT_ID_SELL", 1020))
IB_CLIENT_ID_SYNC = int(os.getenv("IB_CLIENT_ID_SYNC", 1021))  # market data + sync


# ===== Utilities & JSON helpers ===============================================

def _price_or_none(x):
    try:
        v = float(x)
        return v if v > 0 and math.isfinite(v) else None
    except Exception:
        return None

def _exposure_totals(book: dict) -> dict:
    """
    Dollar notional snapshot from live_trades:
      - allocated: open + pending_sell (use entry or intended_entry)
      - pending:   pending_buy (use intended_entry or entry)
      - total:     allocated + pending
    """
    allocated = pending = 0.0
    if not isinstance(book, dict):
        return {"allocated": 0.0, "pending": 0.0, "total": 0.0}

    for bucket in book.values():
        for t in (bucket or {}).get("trades", []):
            sh = int(t.get("shares") or 0)
            if sh <= 0:
                continue
            st = t.get("status")
            if st in ("open", "pending_sell"):
                px = _price_or_none(t.get("entry_price")) or _price_or_none(t.get("intended_entry_price"))
                if px: allocated += px * sh
            elif st == "pending_buy":
                px = _price_or_none(t.get("intended_entry_price")) or _price_or_none(t.get("entry_price"))
                if px: pending += px * sh

    total = allocated + pending
    return {"allocated": float(allocated), "pending": float(pending), "total": float(total)}

def _available_capital(book: dict, roof: float = None) -> float:
    if roof is None:
        roof = float(CAPITAL_LIMIT)
    snap = _exposure_totals(book)
    return max(0.0, float(roof) - snap["total"])

def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)

def _finite_or_none(x) -> Optional[float]:
    try:
        x = float(x)
    except Exception:
        return None
    return x if math.isfinite(x) else None

def _safe_int(x, default: int = 0) -> int:
    try:
        if x is None:
            return default
        xf = float(x)
        if not math.isfinite(xf):
            return default
        return int(xf)
    except Exception:
        return default

def load_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log(f"⚠️ Failed to load {path}: {e}", "WARNING", "BacktestEngine")
    return {} if path.endswith(".json") else None

def write_json_file(path, data):
    # NOTE: your utils.atomic_save_json signature is (data, path)
    if isinstance(path, (dict, list)) and isinstance(data, str):
        log(f"🛠️ write_json_file auto-swapped args (path<-{data})", "WARNING", "BacktestEngine")
        path, data = data, path
    if not isinstance(path, str):
        raise TypeError(f"write_json_file expected path:str, got {type(path)} value={repr(path)[:120]}")
    atomic_save_json(data, path)

def _today_key() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

def _ensure_today_bucket(live_trades: Dict[str, Any]) -> Dict[str, Any]:
    key = _today_key()
    if not isinstance(live_trades, dict):
        live_trades = {}
    if key not in live_trades or not isinstance(live_trades.get(key), dict):
        live_trades[key] = {"trades": []}
    else:
        if "trades" not in live_trades[key] or not isinstance(live_trades[key]["trades"], list):
            live_trades[key]["trades"] = []
    return live_trades[key]

def _already_trading_symbol(live_trades: Dict[str, Any], symbol: str) -> bool:
    for day_bucket in live_trades.values():
        for t in day_bucket.get("trades", []):
            if t.get("symbol") == symbol and t.get("status") in ("open","pending_buy","pending_sell"):
                return True
    return False


# ===== Market sessions =========================================================
def market_is_open() -> bool:
    now = datetime.now(LOCAL_TZ)
    if now.date() in us_holidays:
        return False
    return now.weekday() < 5 and dtime(9, 35) <= now.time() <= dtime(15, 30)


# ===== Polygon freshness guard ================================================
def _parse_polygon_ts(obj) -> Optional[datetime]:
    """
    Attempts to parse several possible timestamp fields from polygon_utils quote dict.
    Accepts epoch (ms or s) or ISO strings.
    """
    if not isinstance(obj, dict):
        return None
    for k in ("timestamp", "last_timestamp", "updated", "t", "time"):
        if k in obj and obj[k] is not None:
            v = obj[k]
            try:
                if isinstance(v, (int, float)):
                    if v > 10_000_000_000:
                        secs = v / 1000.0
                    else:
                        secs = float(v)
                    return datetime.fromtimestamp(secs, tz=dtimezone.utc).astimezone(LOCAL_TZ)
                if isinstance(v, str):
                    try:
                        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    except Exception:
                        dt = pd.to_datetime(v, utc=True).to_pydatetime()
                    if dt.tzinfo is None:
                        dt = LOCAL_TZ.localize(dt)
                    else:
                        dt = dt.astimezone(LOCAL_TZ)
                    return dt
            except Exception:
                continue
    return None

def _quote_delay_seconds(q: dict) -> Optional[float]:
    ts = _parse_polygon_ts(q)
    if not ts:
        return None
    now = datetime.now(LOCAL_TZ)
    return max(0.0, (now - ts).total_seconds())

def _parse_iso_ts(ts: str):
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = LOCAL_TZ.localize(dt)
        return dt
    except Exception:
        return None

def _quote_delay_seconds_from_quote(q: dict) -> Optional[float]:
    ts = q.get("timestamp")
    if not ts:
        return None
    dt = _parse_iso_ts(ts)
    if not dt:
        return None
    return max(0.0, (datetime.now(LOCAL_TZ) - dt).total_seconds())

def _resolve_polygon_freshness() -> float:
    try:
        lp = load_json(LIVE_PARAMS_PATH) or {}
        return float(lp.get("polygon_max_delay_sec", POLYGON_MAX_DELAY_SEC_DEFAULT))
    except Exception:
        return POLYGON_MAX_DELAY_SEC_DEFAULT

def get_live_price(symbol: str, max_delay_sec: float = POLYGON_MAX_DELAY_SEC_DEFAULT) -> Optional[float]:
    """
    Polygon-only, freshness-gated last price for TRADING decisions.
    If stale (> max_delay_sec) or invalid -> return None so callers SKIP the symbol.
    Writes a flat { "SYM": price, ... } map for the dashboard (not used in logic).
    Also updates the consecutive-delay tracker in polygon_utils.
    """
    try:
        q = get_polygon_quote(symbol)
        if not isinstance(q, dict):
            return None

        delay = _quote_delay_seconds_from_quote(q)
        if delay is None:
            # Treat unknown as stale; record it for the delay tracker
            record_quote_delay(symbol, delay_sec=None)
            return None

        # Update the delay tracker (this is how we learn "trash symbols")
        record_quote_delay(symbol, delay_sec=delay)

        if delay > max_delay_sec:
            print(f"⚠️ get_live_price {symbol} stale: delay {delay:.1f}s > {max_delay_sec}s")
            return None

        last = q.get("last")
        if last is None:
            return None
        try:
            last = float(last)
        except Exception:
            return None
        if not (last > 0):
            return None

        # Dashboard-only persistence (not used for trading decisions)
        try:
            live_prices = {}
            if os.path.exists(LIVE_PRICES_PATH):
                with open(LIVE_PRICES_PATH, "r", encoding="utf-8") as f:
                    live_prices = json.load(f)
            live_prices[symbol] = round(last, 6)
            atomic_save_json(live_prices, LIVE_PRICES_PATH)
        except Exception:
            pass

        return last

    except Exception:
        return None

def collect_live_price(symbol: str, price: Optional[float] = None, max_delay_sec: float = POLYGON_MAX_DELAY_SEC_DEFAULT) -> None:
    """
    Persist latest price for dashboard in LIVE_PRICES_PATH as {SYMBOL: last}, only if finite.
    """
    try:
        if price is None:
            price = get_live_price(symbol, max_delay_sec=max_delay_sec)
        if not _is_finite_number(price) or price <= 0:
            return
        live_prices = load_json(LIVE_PRICES_PATH) or {}
        live_prices[symbol] = float(price)
        atomic_save_json(live_prices, LIVE_PRICES_PATH)
    except Exception:
        pass


# ===== IBKR market data & orders ==============================================
def _ib_refresh_open_orders(ib):
    try:
        ib.reqOpenOrders()
        ib.sleep(0.25)
    except Exception:
        pass

def _cancel_order_by_ref(order_ref: str, client_id: int) -> bool:
    """
    Cancel any open/pre-submitted order that matches orderRef on this client_id.
    Returns True if we issued at least one cancel.
    """
    if not order_ref:
        return False
    try:
        from ib_insync import IB
    except Exception as e:
        log(f"❌ _cancel_order_by_ref unavailable: {e}", "ERROR", "IBKR")
        return False

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        _ib_refresh_open_orders(ib)

        did = False
        for tr in ib.trades():
            ref  = getattr(tr.order, "orderRef", None)
            stat = (getattr(tr.orderStatus, "status", "") or "").lower()
            if ref == order_ref and stat in ("submitted", "presubmitted", "pendingsubmit", "inactive"):
                ib.cancelOrder(tr.order)
                did = True
        if did:
            ib.sleep(0.25)
            log(f"🛑 IBKR cancel by orderRef={order_ref} (clientId={client_id})", "INFO", "IBKR")
        else:
            log(f"⚠️ _cancel_order_by_ref: orderRef={order_ref} not found on client {client_id}", "WARNING", "IBKR")
        return did
    except Exception as e:
        log(f"❌ _cancel_order_by_ref error: {e}", "ERROR", "IBKR")
        return False
    finally:
        try: ib.disconnect()
        except Exception: pass


def _cancel_any_buy_for_symbol(symbol: str, client_id: int) -> bool:
    """
    Best-effort: cancel ANY live BUY for this symbol on this client_id.
    Returns True if we issued at least one cancel.
    """
    if not symbol:
        return False
    try:
        from ib_insync import IB
    except Exception as e:
        log(f"❌ _cancel_any_buy_for_symbol unavailable: {e}", "ERROR", "IBKR")
        return False

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        _ib_refresh_open_orders(ib)

        did = False
        for tr in ib.trades():
            sym  = getattr(getattr(tr, "contract", None), "symbol", None)
            act  = (getattr(getattr(tr, "order", None), "action", "") or "").upper()
            stat = (getattr(getattr(tr, "orderStatus", None), "status", "") or "").lower()
            if sym == symbol and act == "BUY" and stat in ("submitted", "presubmitted", "pendingsubmit", "inactive"):
                ib.cancelOrder(tr.order)
                did = True

        if did:
            ib.sleep(0.25)
            log(f"🛑 IBKR cancel ANY BUY for {symbol} (clientId={client_id})", "INFO", "IBKR")
        else:
            log(f"⚠️ _cancel_any_buy_for_symbol: no live BUY for {symbol} on client {client_id}", "WARNING", "IBKR")
        return did
    except Exception as e:
        log(f"❌ _cancel_any_buy_for_symbol error: {e}", "ERROR", "IBKR")
        return False
    finally:
        try: ib.disconnect()
        except Exception: pass

def _cancel_order(order_id: int, client_id: int, order_ref: Optional[str] = None) -> bool:
    """
    Cancel an IBKR order by orderId; fall back to orderRef. Refreshes cache first.
    Returns True if we issued a cancel to at least one handle.
    """
    try:
        from ib_insync import IB
    except Exception as e:
        log(f"❌ _cancel_order unavailable: {e}", "ERROR", "IBKR")
        return False

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        _ib_refresh_open_orders(ib)

        # 1) try by orderId
        trg = _find_trades_by_order_id(ib, order_id)
        if trg:
            ok = _cancel_trades(ib, trg)
            if ok:
                log(f"🛑 IBKR cancel orderId={order_id} (clientId={client_id})", "INFO", "IBKR")
                return True

        # 2) try by orderRef (if we have it)
        if order_ref:
            trg = _find_trades_by_order_ref(ib, order_ref)
            if trg:
                ok = _cancel_trades(ib, trg)
                if ok:
                    log(f"🛑 IBKR cancel by orderRef={order_ref} (clientId={client_id})", "INFO", "IBKR")
                    return True

        # 3) last resort: ask TWS for all open orders (requires setting in TWS API)
        try:
            ib.reqAllOpenOrders()
            ib.sleep(0.5)
            trg = _find_trades_by_order_id(ib, order_id)
            if not trg and order_ref:
                trg = _find_trades_by_order_ref(ib, order_ref)
            if trg:
                ok = _cancel_trades(ib, trg)
                if ok:
                    log(f"🛑 IBKR cancel (all-open-orders) orderId={order_id} / orderRef={order_ref}", "INFO", "IBKR")
                    return True
        except Exception:
            pass

        # Not found under this client
        log(f"⚠️ _cancel_order: orderId={order_id} not found (clientId={client_id})", "WARNING", "IBKR")
        return False

    except Exception as e:
        log(f"❌ _cancel_order error: {e}", "ERROR", "IBKR")
        return False
    finally:
        try: ib.disconnect()
        except Exception: pass

def _cancel_order_any_client(order_id: int, order_ref: Optional[str] = None) -> bool:
    """
    Try to cancel orderId under each configured clientId (BUY/SELL/SYNC).
    """
    if order_id is None:
        return False
    for cid in (IB_CLIENT_ID_BUY, IB_CLIENT_ID_SELL, IB_CLIENT_ID_SYNC):
        if _cancel_order(order_id, cid, order_ref=order_ref):
            return True
    return False


def ibkr_nbbo_last(symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Return (bid, ask, last) snapshot from IBKR (SMART, USD), filtering out NaN.
    """
    try:
        from ib_insync import IB, Stock
    except Exception as e:
        log(f"⚠️ ibkr_nbbo_last unavailable: {e}", "WARNING", "IBKR")
        return None, None, None

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID_SYNC)
        c = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(c)
        ticks = ib.reqTickers(c)
        if not ticks:
            return None, None, None
        t = ticks[0]

        bid = _finite_or_none(getattr(t, "bid", None))
        ask = _finite_or_none(getattr(t, "ask", None))
        last = _finite_or_none(getattr(t, "last", None))

        if last is None and (bid is not None and ask is not None):
            last = (bid + ask) / 2.0 if _is_finite_number(bid) and _is_finite_number(ask) else None

        return bid, ask, last
    except Exception as e:
        log(f"⚠️ ibkr_nbbo_last error for {symbol}: {e}", "WARNING", "IBKR")
        return None, None, None
    finally:
        try: ib.disconnect()
        except Exception: pass

def _find_trades_by_order_id(ib, order_id: int):
    trg = []
    for tr in ib.trades():
        if getattr(tr.order, "orderId", None) == order_id:
            trg.append(tr)
    return trg

def _find_trades_by_order_ref(ib, order_ref: Optional[str]):
    if not order_ref:
        return []
    trg = []
    for tr in ib.trades():
        if getattr(tr.order, "orderRef", None) == order_ref:
            trg.append(tr)
    return trg

def _cancel_trades(ib, trades) -> bool:
    did = False
    for tr in trades:
        st = (getattr(tr.orderStatus, "status", "") or "").lower()
        if st in ("submitted", "presubmitted", "pendingsubmit", "inactive"):
            ib.cancelOrder(tr.order)
            did = True
    if did:
        ib.sleep(0.25)
    return did

def _cancel_and_replace_limit(old_order_id: int, symbol: str, action: str, new_limit: float, qty: int, client_id: int) -> Optional[int]:
    try:
        from ib_insync import IB, Stock, LimitOrder
    except Exception as e:
        log(f"❌ IBKR modify not available: {e}", "ERROR", "IBKR")
        return None
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        for tr in ib.trades():
            if getattr(tr.order, "orderId", None) == old_order_id:
                ib.cancelOrder(tr.order)
                ib.sleep(0.2)
                break
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        new_order = LimitOrder(action.upper(), int(qty), float(new_limit))
        new_order.orderRef = f"engine-{symbol}-{action.upper()}-repl"
        try:
            new_order.outsideRth = True
        except Exception:
            pass
        trade = ib.placeOrder(contract, new_order)
        new_id = getattr(trade.order, "orderId", None)
        log(f"🔁 IBKR {action} replace -> LMT {qty}x {symbol} @ {new_limit:.4f} (old={old_order_id}, new={new_id})", "INFO", "IBKR")
        return new_id
    except Exception as e:
        log(f"❌ IBKR cancel/replace error: {e}", "ERROR", "IBKR")
        return None
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

def _cancel_and_replace_market(old_order_id: int, symbol: str, action: str, qty: int, client_id: int) -> Optional[int]:
    try:
        from ib_insync import IB, Stock, MarketOrder
    except Exception as e:
        log(f"❌ IBKR market replace not available: {e}", "ERROR", "IBKR")
        return None
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        for tr in ib.trades():
            if getattr(tr.order, "orderId", None) == old_order_id:
                ib.cancelOrder(tr.order)
                ib.sleep(0.2)
                break
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        mkt = MarketOrder(action.upper(), int(qty))
        mkt.orderRef = f"engine-{symbol}-{action.upper()}-replMKT"
        try:
            mkt.outsideRth = True
        except Exception:
            pass
        trade = ib.placeOrder(contract, mkt)
        new_id = getattr(trade.order, "orderId", None)
        log(f"🏁 IBKR {action} replace -> MKT {qty}x {symbol} (old={old_order_id}, new={new_id})", "INFO", "IBKR")
        return new_id
    except Exception as e:
        log(f"❌ IBKR market replace error: {e}", "ERROR", "IBKR")
        return None
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

def ibkr_last_order_feedback(order_id: Optional[int], order_ref: Optional[str]) -> Dict[str, Any]:
    """
    Probe Trade status + last log for errors like 'outside price bands', routing restrictions, etc.
    """
    try:
        from ib_insync import IB
    except Exception:
        return {}
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID_SYNC)
        tr, status = None, None
        for t in ib.trades():
            if order_id is not None and getattr(t.order, "orderId", None) == order_id:
                tr, status = t, getattr(t.orderStatus, "status", None)
                break
            if order_ref and getattr(t.order, "orderRef", None) == order_ref:
                tr, status = t, getattr(t.orderStatus, "status", None)
                break
        if not tr:
            return {}
        msg = None
        code = None
        try:
            if tr.log:
                last = tr.log[-1]
                msg = getattr(last, "message", None)
                code = getattr(last, "errorCode", None)
        except Exception:
            pass
        return {"status": (status or "").lower(), "last_error_code": code, "last_message": msg}
    except Exception:
        return {}
    finally:
        try: ib.disconnect()
        except Exception: pass

def _attempt_cancel_pending_buy(trade: dict) -> bool:
    """
    Best-effort cancel for a pending BUY.
    Tries: orderId across all clients -> orderRef on BUY client -> any BUY for symbol.
    Always returns a boolean.
    """
    symbol = trade.get("symbol")
    bo = (trade.get("broker_order") or {})
    oid = bo.get("orderId")
    oref = bo.get("orderRef")

    sent = False
    try:
        if oid is not None:
            sent = _cancel_order_any_client(int(oid), order_ref=oref) or sent
    except Exception:
        pass
    try:
        if not sent and oref:
            sent = _cancel_order_by_ref(oref, IB_CLIENT_ID_BUY) or sent
    except Exception:
        pass
    try:
        if not sent and symbol:
            sent = _cancel_any_buy_for_symbol(symbol, IB_CLIENT_ID_BUY) or sent
    except Exception:
        pass
    return bool(sent)

def _attempt_cancel_pending_sell(trade: dict) -> bool:
    """
    Best-effort cancel for a pending SELL (mirror of BUY path, uses SELL client on orderRef step).
    """
    bo = (trade.get("broker_order_sell") or {})
    oid = bo.get("orderId")
    oref = bo.get("orderRef")

    sent = False
    try:
        if oid is not None:
            sent = _cancel_order_any_client(int(oid), order_ref=oref) or sent
    except Exception:
        pass
    try:
        if not sent and oref:
            sent = _cancel_order_by_ref(oref, IB_CLIENT_ID_SELL) or sent
    except Exception:
        pass
    return bool(sent)


# ===== Gainers loader (robust to schema) ======================================
def load_gainers_symbols() -> Tuple[List[str], List[str]]:
    def _load(p: str):
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            return None
        return None

    data = _load("exports/top_gainers.json") or _load("top_gainers.json")
    if data is None:
        try:
            get_top_gainers_polygon(limit=13, save_json_path="exports/top_gainers.json")
            filter_gainers_for_momentum()
            data = _load("exports/top_gainers.json") or _load("top_gainers.json")
        except Exception:
            data = None

    items: List[dict] = []
    if isinstance(data, dict):
        if isinstance(data.get("gainers"), list):
            items = data["gainers"]
        elif isinstance(data.get("symbols"), list):
            items = data["symbols"]
    elif isinstance(data, list):
        items = data

    def _to_bool(v) -> bool:
        if isinstance(v, bool): return v
        if isinstance(v, (int, float)): return v != 0
        if isinstance(v, str): return v.strip().lower() in {"true","1","yes","y"}
        return False

    momentum, non_momentum, seen = [], [], set()
    for it in items or []:
        sym = it.get("symbol") or it.get("ticker") or it.get("Symbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        flag = it.get("in_momentum")
        if flag is None:
            flag = it.get("momentum")
        if flag is None:
            score = it.get("momentum_score")
            flag = bool(isinstance(score, (int,float)) and score >= 4)
        else:
            flag = _to_bool(flag)
        (momentum if flag else non_momentum).append(sym)

    return momentum, non_momentum


# ===== Exit rules (kept for potential reuse) ==================================
@dataclass
class ExitDecision:
    should_exit: bool
    reason: str
    intended_price: Optional[float]

def _negative_exit_block_secs() -> int:
    lp = load_json(LIVE_PARAMS_PATH)
    return int(lp.get("negative_exit_block_secs", NEGATIVE_EXIT_BLOCK_SECS_DEFAULT))


# ===== Inline sync: executions -> open/closed =================================
def _format_exec_filter_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%d %H:%M:%S")  # IBKR requires SPACE

def _coerce_int(x) -> Optional[int]:
    try:
        return int(x) if x is not None else None
    except Exception:
        return None

def sync_broker_fills():
    try:
        from ib_insync import IB, ExecutionFilter
    except Exception as e:
        log(f"⚠️ Can't sync fills (ib_insync not available): {e}", "WARNING", "BacktestEngine")
        return

    trades_book = load_json(LIVE_TRADES_PATH)
    if not trades_book:
        return

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID_SYNC)
        midnight_local = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        ef = ExecutionFilter(time=_format_exec_filter_time(midnight_local))
        fills = ib.reqExecutions(ef)

        by_order: Dict[int, Dict[str, Any]] = {}
        for item in fills or []:
            contract = execution = None
            if hasattr(item, "contract") and hasattr(item, "execution"):
                contract = getattr(item, "contract")
                execution = getattr(item, "execution")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                contract, execution = item[0], item[1]
            else:
                continue

            oid = _coerce_int(getattr(execution, "orderId", None))
            side = getattr(execution, "side", None)
            shares = _safe_int(getattr(execution, "shares", 0), 0)
            price = _finite_or_none(getattr(execution, "price", 0.0)) or 0.0
            ex_time = getattr(execution, "time", "")

            if oid is None:
                continue

            d = by_order.setdefault(oid, {"cumQty": 0, "value": 0.0, "side": side, "lastTime": ex_time, "symbol": getattr(contract, "symbol", None)})
            d["cumQty"] += _safe_int(shares, 0)
            d["value"]  += float(price) * _safe_int(shares, 0)
            try:
                if str(ex_time) > str(d["lastTime"]):
                    d["lastTime"] = ex_time
            except Exception:
                pass

        positions = {getattr(p.contract, "symbol", None): p for p in ib.positions()}

        changed = False
        prom_buys = prom_sells = 0
        for _, bucket in trades_book.items():
            for t in bucket.get("trades", []):
                if t.get("status") == "pending_buy":
                    orderId = _coerce_int(t.get("broker_order", {}).get("orderId"))
                    shares  = _safe_int(t.get("shares"), 0)
                    rec = by_order.get(orderId) if orderId is not None else None
                    if rec and rec.get("side") == "BOT" and rec.get("cumQty", 0) >= shares:
                        avg = (rec["value"] / rec["cumQty"]) if rec["cumQty"] else None
                        t["status"] = "open"
                        if avg:
                            t["entry_price"] = float(avg); t["entry_price_source"] = "ibkr_exec_avg"
                        else:
                            pos = positions.get(t.get("symbol"))
                            if pos and getattr(pos, "avgCost", None):
                                t["entry_price"] = float(pos.avgCost); t["entry_price_source"] = "ibkr_position"
                        prom_buys += 1; changed = True
                    else:
                        pos = positions.get(t.get("symbol"))
                        if pos and abs(_safe_int(getattr(pos, "position", 0), 0)) >= shares:
                            t["status"] = "open"
                            if getattr(pos, "avgCost", None):
                                t["entry_price"] = float(pos.avgCost); t["entry_price_source"] = "ibkr_position"
                            prom_buys += 1; changed = True

                elif t.get("status") == "pending_sell":
                    orderId = _coerce_int(t.get("broker_order_sell", {}).get("orderId"))
                    shares  = _safe_int(t.get("shares"), 0)
                    rec = by_order.get(orderId) if orderId is not None else None
                    if rec and rec.get("side") == "SLD" and rec.get("cumQty", 0) >= shares:
                        avg = (rec["value"] / rec["cumQty"]) if rec["cumQty"] else None
                        exit_price = float(avg) if avg else None
                        t["exit_time"] = datetime.now(LOCAL_TZ).isoformat()
                        if exit_price is not None:
                            t["exit_price"] = exit_price
                            ep = t.get("entry_price") or t.get("intended_entry_price") or 0.0
                            sh = _safe_int(t.get("shares"), 0)
                            t["pnl"] = round((exit_price - ep) * sh, 2) if ep else 0.0
                            t["pnl_pct"] = ((exit_price - ep) / ep) * 100 if ep else 0.0
                        t["status"] = "closed"
                        prom_sells += 1; changed = True
                    else:
                        pos = positions.get(t.get("symbol"))
                        qty = abs(_safe_int(getattr(pos, "position", 0), 0)) if pos else 0
                        if qty == 0:
                            t["exit_time"] = datetime.now(LOCAL_TZ).isoformat()
                            lp = get_market_price(t.get("symbol"), polygon_first=True)
                            if lp:
                                t["exit_price"] = float(lp)
                                ep = t.get("entry_price") or t.get("intended_entry_price") or 0.0
                                sh = _safe_int(t.get("shares"), 0)
                                t["pnl"] = round((lp - ep) * sh, 2) if ep else 0.0
                                t["pnl_pct"] = ((lp - ep) / ep) * 100 if ep else 0.0
                            t["status"] = "closed"
                            prom_sells += 1; changed = True

        if changed:
            write_json_file(LIVE_TRADES_PATH, trades_book)
            log(f"🔄 Sync: promoted {prom_buys} buy(s) to open, {prom_sells} sell(s) to closed.", "INFO", "BacktestEngine")
    except Exception as e:
        log(f"⚠️ sync_broker_fills error: {e}", "WARNING", "BacktestEngine")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


# ===== Get a market price (Polygon first, IBKR fallback) ======================
def get_market_price(symbol: str,
                     polygon_first: bool = True,
                     polygon_max_delay_sec: Optional[float] = None) -> Optional[float]:
    """
    Return a *fresh* market price. Prefer Polygon (fresh-only), fallback to IBKR.
    """
    if polygon_max_delay_sec is None:
        polygon_max_delay_sec = _resolve_polygon_freshness()

    def _polygon() -> Optional[float]:
        px = get_live_price(symbol, max_delay_sec=polygon_max_delay_sec)
        return float(px) if _is_finite_number(px) and px > 0 else None

    def _ibkr() -> Optional[float]:
        _, _, last = ibkr_nbbo_last(symbol)
        return float(last) if _is_finite_number(last) and last > 0 else None

    if polygon_first:
        px = _polygon()
        if px is not None:
            return px
        return _ibkr()
    else:
        px = _ibkr()
        if px is not None:
            return px
        return _polygon()


# ===== Pending Watchdog (single, clean logic) =================================
def manage_pending_orders():
    book = load_json(LIVE_TRADES_PATH)
    if not book:
        return

    lp = load_json(LIVE_PARAMS_PATH) or {}
    buy_cancel_after_s = int((lp.get("pending_policy") or {}).get("cancel_buy_after_s", BUY_CLOG_CANCEL_SECS_DEFAULT))

    changed = False

    def _age_secs(tr):
        try:
            t0s = tr.get("datetime")
            if t0s:
                t0 = datetime.fromisoformat(t0s)
                if t0.tzinfo is None: t0 = LOCAL_TZ.localize(t0)
                return (datetime.now(LOCAL_TZ) - t0).total_seconds()
        except Exception:
            pass
        return 0.0

    for _, bucket in book.items():
        for t in bucket.get("trades", []):
            st = t.get("status")
            if st not in ("pending_buy", "pending_sell"):
                continue

            symbol = t.get("symbol")
            shares = _safe_int(t.get("shares"), 0)
            if not symbol or shares <= 0:
                continue

            age = _age_secs(t)

            # -------- PENDING BUY --------
            if st == "pending_buy":
                # Timeout unclog
                if age >= buy_cancel_after_s:
                    sent = _attempt_cancel_pending_buy(t)
                    t["status"] = "aborted_buy"
                    esc = t.setdefault("pending_escalation", {"stage": "none", "history": []})
                    esc["stage"] = "aborted_timeout"
                    esc["history"].append({
                        "ts": datetime.now(LOCAL_TZ).isoformat(),
                        "action": "cancel_buy_timeout",
                        "age_sec": age,
                        "cancel_sent": bool(sent),
                    })
                    changed = True
                    continue

                intended = _finite_or_none(t.get("intended_entry_price") or 0.0)
                market = get_market_price(symbol, polygon_first=True)
                if intended is None or market is None or market <= 0:
                    continue

                # If intended >= market -> convert to MKT (or marketable LMT)
                if intended >= market:
                    bo = t.get("broker_order", {}) or {}
                    oid = bo.get("orderId")
                    new_id = _cancel_and_replace_market(oid, symbol, "BUY", shares, IB_CLIENT_ID_BUY) if oid is not None else None
                    if new_id is None:
                        resp = _place_marketable_limit_order(symbol, shares, "BUY", IB_CLIENT_ID_BUY, bo.get("orderRef"), slip_bps=80.0)
                        if resp.get("ok"):
                            bo.update({"orderId": resp.get("orderId"), "orderRef": resp.get("orderRef"), "type": "LMT", "price": resp.get("price"), "clientId": IB_CLIENT_ID_BUY})
                            t["broker_order"] = bo
                            changed = True
                    else:
                        bo["orderId"] = new_id; bo["type"] = "MKT"; bo["price"] = None; bo["clientId"] = IB_CLIENT_ID_BUY
                        t["broker_order"] = bo
                        changed = True
                else:
                    # intended < market -> abort (avoid bad entry)
                    sent = _attempt_cancel_pending_buy(t)
                    t["status"] = "aborted_buy"
                    esc = t.setdefault("pending_escalation", {"stage": "none", "history": []})
                    esc["stage"] = "aborted_above_market"
                    esc["history"].append({
                        "ts": datetime.now(LOCAL_TZ).isoformat(),
                        "action": "abort_buy_above_market",
                        "intended": float(intended),
                        "market": float(market),
                        "cancel_sent": bool(sent),
                    })
                    changed = True
                    continue

            # -------- PENDING SELL --------
            elif st == "pending_sell":
                intended = _finite_or_none(t.get("intended_exit_price") or t.get("entry_price") or 0.0)
                market = get_market_price(symbol, polygon_first=True)
                if intended is None or market is None or market <= 0:
                    continue

                # Hold time since entry
                hold_secs = 0
                try:
                    t0s = t.get("datetime")
                    if t0s:
                        t0 = datetime.fromisoformat(t0s)
                        if t0.tzinfo is None:
                            t0 = LOCAL_TZ.localize(t0)
                        hold_secs = (datetime.now(LOCAL_TZ) - t0).total_seconds()
                except Exception:
                    pass

                sell_now = (float(intended) <= market) or (hold_secs >= _negative_exit_block_secs())
                if not sell_now:
                    continue

                bos = t.get("broker_order_sell", {}) or {}
                oid = bos.get("orderId")

                new_id = _cancel_and_replace_market(oid, symbol, "SELL", shares, IB_CLIENT_ID_SELL) if oid is not None else None
                if new_id is not None:
                    bos.update({"orderId": new_id, "type": "MKT", "price": None, "clientId": IB_CLIENT_ID_SELL})
                    t["broker_order_sell"] = bos
                    changed = True
                    continue

                fb = ibkr_last_order_feedback(oid, bos.get("orderRef"))
                msg = (fb.get("last_message") or "").lower()
                stt = (fb.get("status") or "").lower()
                if stt in ("inactive", "rejected", "cancelled") or "outside" in msg or "price band" in msg:
                    resp = _place_marketable_limit_order(symbol, shares, "SELL", IB_CLIENT_ID_SELL, bos.get("orderRef"), slip_bps=80.0)
                    if resp.get("ok"):
                        bos.update({"orderId": resp.get("orderId"), "orderRef": resp.get("orderRef"), "type": "LMT", "price": resp.get("price"), "clientId": IB_CLIENT_ID_SELL})
                        t["broker_order_sell"] = bos
                        changed = True

    if changed:
        write_json_file(LIVE_TRADES_PATH, book)


# ===== BUY generation (Polygon-first) =========================================
@fail_safe(module="BacktestEngine", critical=True)
def generate_buy_orders():
    if not market_is_open():
        log("Market closed.", "INFO", "BacktestEngine")
        return
    if datetime.now(LOCAL_TZ).time() >= dtime(15, 30):
        log("🛑 Market closed at 15:30, skipping BUY generation.", "INFO", "BacktestEngine")
        return

    live_params = load_json(LIVE_PARAMS_PATH)
    control = load_json(CONTROL_PATH)
    live_trades = load_json(LIVE_TRADES_PATH) or {}

    # min shares threshold (configurable)
    min_shares_buy = int(live_params.get("min_buy_shares", MIN_BUY_SHARES_DEFAULT))

    # ⛔ Global capital roof check (never exceed CAPITAL_LIMIT)
    avail_cap = _available_capital(live_trades, roof=CAPITAL_LIMIT)
    if avail_cap <= 0:
        log(f"⛔ Capital roof reached: exposure at/over ${CAPITAL_LIMIT:,.2f}. Skipping buys.", "INFO", "BacktestEngine")
        return

    max_pending_total = int(live_params.get("pending_policy", {}).get("max_pending_total", MAX_PENDING_TOTAL_DEFAULT))
    current_pending_buys = sum(1 for _, b in (live_trades or {}).items() for t in b.get("trades", []) if t.get("status") == "pending_buy")
    if current_pending_buys >= max_pending_total:
        log(f"⛔ Too many pending buys ({current_pending_buys} >= {max_pending_total}), skipping new buys.", "INFO", "BacktestEngine")
        return

    min_confidence = float(live_params.get("min_confidence", MIN_CONFIDENCE))
    alloc = live_params.get("capital_allocation", {})
    base_pct = float(alloc.get("base_pct", 0.1))
    bonus_pct = float(alloc.get("bonus_pct", 0.2))
    max_allocation_pct = float(alloc.get("max_allocation_pct", 0.4))
    position_cap = float(live_params.get("position_cap_dollars", CAPITAL_LIMIT))

    suspended = set(control.get("suspended_symbols", []))

    momentum_syms, non_momentum_syms = load_gainers_symbols()
    if not momentum_syms and not non_momentum_syms:
        log("❌ No symbols available to trade from top_gainers.json.", "ERROR", "BacktestEngine")
        return

    today_bucket = _ensure_today_bucket(live_trades)
    candidates = momentum_syms + [s for s in non_momentum_syms if s not in momentum_syms][:4]

    for symbol in candidates:
        if symbol in suspended:
            continue
        if _already_trading_symbol(live_trades, symbol):
            continue

        try:
            sig = get_strategy_signals(symbol) or {}
        except Exception:
            sig = {}
        conf = float(sig.get("confidence", 0.8))
        if conf < min_confidence:
            continue

        market = get_market_price(symbol, polygon_first=True)
        if market is None or not _is_finite_number(market) or market <= 0:
            continue
        collect_live_price(symbol, price=market)

        # ⛔ Recompute remaining capital **right before sizing this order**
        avail_cap = _available_capital(live_trades, roof=CAPITAL_LIMIT)
        if avail_cap < market:   # not enough to buy a single share
            log(f"⛔ Roof guard: only ${avail_cap:,.2f} available; need ≥ ${market:,.2f} for 1 share of {symbol}. Skipping.", "INFO", "BacktestEngine")
            break

        intended_entry_price = _finite_or_none(sig.get("entry_limit"))
        if intended_entry_price is None:
            intended_entry_price = market

        # BUY rule: only buy if intended >= market (else we may be late to the move)
        if intended_entry_price > market:
            log(f"⏭️ Skip BUY {symbol}: market {market:.4f} > intended {float(intended_entry_price):.4f}", "INFO", "BacktestEngine")
            continue

        alloc_pct = min(base_pct + (bonus_pct if conf >= 0.8 else 0.0), max_allocation_pct)
        strat_dollars = min(CAPITAL_LIMIT * alloc_pct, position_cap)
        dollars = min(strat_dollars, avail_cap)
        if not _is_finite_number(dollars) or dollars <= 0 or market <= 0:
            continue
        raw_shares = dollars / market
        if not _is_finite_number(raw_shares) or raw_shares <= 0:
            continue
        shares = _safe_int(raw_shares, 0)
        if shares < min_shares_buy:
            log(f"⏭️ Skip BUY {symbol}: shares {shares} < min {min_shares_buy}.", "INFO", "BacktestEngine")
            continue

        order_resp = _place_ibkr_market_order(symbol, shares, "BUY", client_id=IB_CLIENT_ID_BUY, order_ref=f"engine-{symbol}-BUY")
        trade = {
            "symbol": symbol,
            "datetime": datetime.now(LOCAL_TZ).isoformat(),
            "status": "pending_buy" if order_resp.get("ok") else "error_buy",
            "shares": shares,
            "intended_entry_price": float(intended_entry_price),
            "broker_order": {
                "action": "BUY", "type": "MKT", "price": None,
                "orderId": order_resp.get("orderId"), "orderRef": order_resp.get("orderRef"),
                "status": order_resp.get("status"), "error": order_resp.get("error"),
                "clientId": IB_CLIENT_ID_BUY,
            },
            "confidence": conf,
        }
        today_bucket["trades"].append(trade)
        write_json_file(LIVE_TRADES_PATH, live_trades)

        orders = load_json(BUY_ORDERS_PATH) or []
        orders.append({
            "ts": datetime.now(LOCAL_TZ).isoformat(),
            "symbol": symbol, "action": "BUY", "price": None,
            "shares": shares, "orderId": order_resp.get("orderId"),
            "orderRef": order_resp.get("orderRef"), "status": order_resp.get("status"),
            "type": "MKT",
        })
        write_json_file(BUY_ORDERS_PATH, orders)


# ===== SELL generation (Polygon-first) ========================================
@fail_safe(module="BacktestEngine", critical=True)
def generate_sell_orders():
    live_trades = load_json(LIVE_TRADES_PATH) or {}
    exit_params = load_json(EXIT_PARAMS_PATH)
    changed = False
    if datetime.now(LOCAL_TZ).time() >= dtime(16, 0):
        log("🛑 Market closed at 16:00, skipping SELL generation.", "INFO", "BacktestEngine")
        return
    for _, bucket in live_trades.items():
        for trade in bucket.get("trades", []):
            if trade.get("status") != "open":
                continue

            symbol = trade.get("symbol")
            if not symbol:
                continue

            market = get_market_price(symbol, polygon_first=True)
            if market is None or not _is_finite_number(market) or market <= 0:
                collect_live_price(symbol, max_delay_sec=_resolve_polygon_freshness())
                continue
            collect_live_price(symbol, price=market)

            intended = trade.get("intended_exit_price")
            if intended is None:
                entry_ = _finite_or_none(trade.get("entry_price") or trade.get("intended_entry_price") or market)
                if entry_ is None or entry_ <= 0:
                    entry_ = market
                tp_pct = float(exit_params.get("take_profit_pct", 0.6)) / 100.0
                intended = entry_ * (1 + (tp_pct if tp_pct > 0 else 0.0025))
                trade["intended_exit_price"] = float(intended)
            else:
                intended = _finite_or_none(intended)
                if intended is None or intended <= 0:
                    intended = market
                    trade["intended_exit_price"] = float(intended)

            hold_secs = 0
            try:
                t0s = trade.get("datetime")
                if t0s:
                    t0 = datetime.fromisoformat(t0s)
                    if t0.tzinfo is None:
                        t0 = LOCAL_TZ.localize(t0)
                    hold_secs = (datetime.now(LOCAL_TZ) - t0).total_seconds()
            except Exception:
                pass

            sell_now = (float(intended) <= market) or (hold_secs >= _negative_exit_block_secs())
            if not sell_now:
                continue

            shares = _safe_int(trade.get("shares"), 0)
            if shares <= 0:
                continue

            resp = _place_ibkr_market_order(symbol, shares, "SELL", client_id=IB_CLIENT_ID_SELL, order_ref=f"engine-{symbol}-SELL")
            trade["exit_reason"] = "market_ge_intended" if float(intended) <= market else "market_after_30m"
            trade["status"] = "pending_sell" if resp.get("ok") else "error_sell"
            trade["broker_order_sell"] = {
                            "action": "SELL", "type": "MKT", "price": None,
                            "orderId": resp.get("orderId"), "orderRef": resp.get("orderRef"),
                            "status": resp.get("status"), "error": resp.get("error"),
                            "clientId": IB_CLIENT_ID_SELL,
                        }
            changed = True

            # persist live_trades update
            write_json_file(LIVE_TRADES_PATH, live_trades)

            # append to SELL orders log
            sell_orders = load_json(SELL_ORDERS_PATH) or []
            sell_orders.append({
                            "ts": datetime.now(LOCAL_TZ).isoformat(),
                            "symbol": symbol, "action": "SELL", "price": None,
                            "shares": shares, "orderId": resp.get("orderId"),
                            "orderRef": resp.get("orderRef"), "status": resp.get("status"),
                            "type": "MKT",
                        })
            write_json_file(SELL_ORDERS_PATH, sell_orders)

            if changed:
                write_json_file(LIVE_TRADES_PATH, live_trades)
                        

# ===== Order placers (market + marketable limit) ===============================

def _place_ibkr_market_order(symbol: str, qty: int, action: str, client_id: int, order_ref: Optional[str] = None) -> Dict[str, Any]:
    """
    Place a pure MARKET order via IBKR (SMART/USD). Returns a dict with ok, orderId, orderRef, status, error.
    """
    try:
        from ib_insync import IB, Stock, MarketOrder
    except Exception as e:
        log(f"❌ ib_insync not available: {e}", "ERROR", "IBKR")
        return {"ok": False, "orderId": None, "orderRef": order_ref, "status": "failed", "error": str(e)}

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        order = MarketOrder(action.upper(), int(qty))
        order.orderRef = order_ref or f"engine-{symbol}-{action.upper()}-MKT"
        try:
            order.outsideRth = True
        except Exception:
            pass
        tr = ib.placeOrder(contract, order)
        orderId = getattr(tr.order, "orderId", None)
        log(f"🚀 IBKR {action.upper()} {qty}x {symbol} @ MKT (orderId={orderId}, clientId={client_id})", "INFO", "IBKR")
        return {"ok": True, "orderId": orderId, "orderRef": order.orderRef, "status": "submitted", "error": None}
    except Exception as e:
        log(f"❌ IBKR market order error: {e}", "ERROR", "IBKR")
        return {"ok": False, "orderId": None, "orderRef": order_ref, "status": "failed", "error": str(e)}
    finally:
        try: ib.disconnect()
        except Exception: pass


def _place_marketable_limit_order(symbol: str,
                                  qty: int,
                                  action: str,
                                  client_id: int,
                                  order_ref: Optional[str] = None,
                                  slip_bps: float = 60.0) -> Dict[str, Any]:
    """
    If MARKET fails (e.g., price bands / routing), place a marketable LIMIT:
      - BUY  limit = (ask or last) * (1 + slip_bps/10000)
      - SELL limit = (bid or last) * (1 - slip_bps/10000)
    """
    try:
        from ib_insync import IB, Stock, LimitOrder
    except Exception as e:
        log(f"❌ ib_insync not available: {e}", "ERROR", "IBKR")
        return {"ok": False, "orderId": None, "orderRef": order_ref, "status": "failed", "error": str(e), "price": None}

    # try to get a reference price
    bid, ask, last = ibkr_nbbo_last(symbol)
    if action.upper() == "BUY":
        ref = ask or last
        if not _is_finite_number(ref) or ref <= 0:
            return {"ok": False, "orderId": None, "orderRef": order_ref, "status": "failed", "error": "no_ask_or_last", "price": None}
        limit_px = float(ref) * (1.0 + float(slip_bps) / 10000.0)
    else:  # SELL
        ref = bid or last
        if not _is_finite_number(ref) or ref <= 0:
            return {"ok": False, "orderId": None, "orderRef": order_ref, "status": "failed", "error": "no_bid_or_last", "price": None}
        limit_px = float(ref) * (1.0 - float(slip_bps) / 10000.0)

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        c = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(c)
        lmt = LimitOrder(action.upper(), int(qty), float(limit_px))
        lmt.orderRef = order_ref or f"engine-{symbol}-{action.upper()}-LMTmktbl"
        try:
            lmt.outsideRth = True
        except Exception:
            pass
        tr = ib.placeOrder(c, lmt)
        oid = getattr(tr.order, "orderId", None)
        log(f"🧩 IBKR {action.upper()} {qty}x {symbol} @ LMT {limit_px:.4f} (marketable) orderId={oid} clientId={client_id}", "INFO", "IBKR")
        return {"ok": True, "orderId": oid, "orderRef": lmt.orderRef, "status": "submitted", "error": None, "price": float(limit_px)}
    except Exception as e:
        log(f"❌ IBKR marketable LIMIT error: {e}", "ERROR", "IBKR")
        return {"ok": False, "orderId": None, "orderRef": order_ref, "status": "failed", "error": str(e), "price": None}
    finally:
        try: ib.disconnect()
        except Exception: pass


# ===== Main loop ===============================================================

@fail_safe(module="BacktestEngine", critical=True)
def run_loop():
    # Sync fills first (promote pending_* to open/closed when executions happen)
    sync_broker_fills()

    # Manage pendings (unclog, convert, abort)
    manage_pending_orders()

    # Generate exits before new entries (keeps exposure tighter)
    generate_sell_orders()

    # Then try new buys
    generate_buy_orders()

    sync_broker_fills()
    log(f"Sleeping {LOOP_INTERVAL}s before next cycle.", "INFO", "BacktestEngine")


if __name__ == "__main__":
    while True:
        try:
            run_loop()
        except Exception as e:
            log(f"⚠️ Error in BacktestEngine: {e}", "ERROR", "BacktestEngine")
        time.sleep(LOOP_INTERVAL)

