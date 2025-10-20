# backtest_engine.py — IBKR integration with price floor/ceiling, sticky sells, cooldowns,
# robust fill sync (orderRef), fresh-quote guard, and flat live_prices writer.
# - Wallet removed
# - Exact-price LIMIT orders to IBKR
# - Separate client IDs for BUY/SELL/SYNC
# - Manage pendings without breaking your rules:
#     * SELL floor: never execute below intended_exit_price
#     * BUY ceiling: never execute above intended_entry_price
#     * SELLs are STICKY: resting LMT at floor (no revert to open, no market)
#     * Back-pressure only on BUY pendings
#     * Debounce/cooldown broker retries + max tries
# - Negative-PnL exit is blocked for first 30 minutes
# - Live prices written as flat map { "SYM": 123.45 } when fresh

import os
import json
import time
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, time as dtime

import pandas as pd
from pytz import timezone as pytz_timezone
import holidays

from modules.log_utils import log, fail_safe
from utils import atomic_save_json
from modules.model_utils import add_technical_indicators
from modules.ai_signals_pro import get_strategy_signals
from modules.price_data import get_price_data
from polygon_utils import (
    get_top_gainers_polygon,
    filter_gainers_for_momentum,
    get_polygon_quote,
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

# Guards / policy (can be overridden in LIVE_PARAMS)
NEGATIVE_EXIT_BLOCK_SECS_DEFAULT = 1800     # 30 minutes
MAX_PENDING_TOTAL_DEFAULT = 6               # back-pressure

# ===== IBKR connection =====
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4002"))       # TWS paper default (Gateway usually 4001/4002)
IB_CLIENT_ID_BUY  = int(os.getenv("IB_CLIENT_ID_BUY",  1019))
IB_CLIENT_ID_SELL = int(os.getenv("IB_CLIENT_ID_SELL", 1020))
IB_CLIENT_ID_SYNC = int(os.getenv("IB_CLIENT_ID_SYNC", 1021))  # inline sync

# --------------------------------------------------------------------------------------
# Utilities (load/save)
# --------------------------------------------------------------------------------------
def load_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log(f"⚠️ Failed to load {path}: {e}", "WARNING", "BacktestEngine")
    return {}

def write_json_file(path, data):
    # utils.atomic_save_json signature in your repo is (data, path).
    if isinstance(path, (dict, list)) and isinstance(data, str):
        log(f"🛠️ write_json_file auto-swapped args (path<-{data})", "WARNING", "BacktestEngine")
        path, data = data, path
    if not isinstance(path, str):
        raise TypeError(f"write_json_file expected 'path' as str, got {type(path)} with value={repr(path)[:120]}")
    atomic_save_json(data, path)

def _today_key() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

def _ensure_today_bucket(live_trades: Dict[str, Any]) -> Dict[str, Any]:
    key = _today_key()
    if not isinstance(live_trades, dict):
        live_trades.clear()
    if key not in live_trades or not isinstance(live_trades.get(key), dict):
        live_trades[key] = {"trotes": [], "trades": []}  # keep 'trades' compat
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

def market_is_open() -> bool:
    now = datetime.now(LOCAL_TZ)
    if now.date() in us_holidays:
        return False
    return now.weekday() < 5 and dtime(9, 35) <= now.time() <= dtime(15, 30)

# --------------------------------------------------------------------------------------
# Quotes: freshness guard + flat live_prices writer
# --------------------------------------------------------------------------------------
def _quote_delay_seconds(q: dict) -> Optional[float]:
    """
    Try to determine quote staleness in seconds from a Polygon-style dict.
    Accepts various timestamp keys or an explicit delay field.
    """
    if not isinstance(q, dict):
        return None

    # Explicit delay if provided by polygon_utils
    for k in ("delayed_by", "delay", "age_sec", "latency_sec"):
        v = q.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass

    # Derive from timestamp
    ts_val = None
    for k in ("timestamp", "ts", "last_timestamp", "t", "sip_ts", "lastUpdate", "last_trade_ts", "received_at"):
        if k in q:
            ts_val = q.get(k)
            break
    if ts_val is None:
        return None

    try:
        if isinstance(ts_val, (int, float)):
            x = float(ts_val)
            if x > 1e14:      epoch_s = x / 1e9  # ns
            elif x > 1e12:    epoch_s = x / 1e6  # µs
            elif x > 1e10:    epoch_s = x / 1e3  # ms
            else:             epoch_s = x       # s
            ts_dt = datetime.fromtimestamp(epoch_s, tz=LOCAL_TZ)
        else:
            s = str(ts_val).replace("Z", "+00:00")
            try:
                ts_dt = datetime.fromisoformat(s)
                if ts_dt.tzinfo is None:
                    ts_dt = LOCAL_TZ.localize(ts_dt)
                else:
                    ts_dt = ts_dt.astimezone(LOCAL_TZ)
            except Exception:
                ts_dt = pd.to_datetime(s, errors="coerce", utc=True)
                if pd.isna(ts_dt):
                    return None
                ts_dt = ts_dt.tz_convert(LOCAL_TZ).to_pydatetime()
    except Exception:
        return None

    now_dt = datetime.now(LOCAL_TZ)
    return max(0.0, (now_dt - ts_dt).total_seconds())

def get_live_price(symbol: str, max_delay_sec: float = 10.0) -> Optional[float]:
    """
    Get a *fresh* last price from Polygon. If the quote is stale (> max_delay_sec)
    or Polygon fails, return None so callers skip this symbol. No bar fallback.
    """
    try:
        q = get_polygon_quote(symbol)
        if not isinstance(q, dict):
            log(f"⚠️ {symbol}: quote not a dict -> skipping", "WARNING", "BacktestEngine")
            return None

        last = q.get("last")
        try:
            last = float(last) if last is not None else None
        except Exception:
            last = None

        delay = _quote_delay_seconds(q)
        if delay is None:
            log(f"⚠️ {symbol}: could not determine quote freshness -> skipping", "WARNING", "BacktestEngine")
            return None
        if delay > max_delay_sec:
            log(f"⚠️ {symbol}: quote delayed by {delay:.2f}s (> {max_delay_sec}s) -> skipping", "WARNING", "BacktestEngine")
            return None
        if last is None or pd.isna(last):
            log(f"⚠️ {symbol}: missing 'last' in quote -> skipping", "WARNING", "BacktestEngine")
            return None

        return float(last)

    except Exception as e:
        log(f"⚠️ get_polygon_quote error for {symbol}: {e} -> skipping", "WARNING", "BacktestEngine")
        return None

def collect_live_price(symbol: str, max_delay_sec: float = 10.0):
    """
    Update exports/live_prices.json in flat format: { "AAPL": 227.12, ... }.
    Only writes when the Polygon quote is fresh (<= max_delay_sec).
    Returns float price on success, or None if skipped.
    """
    live_prices = load_json(LIVE_PRICES_PATH) or {}
    try:
        q = get_polygon_quote(symbol)
    except Exception as e:
        log(f"⚠️ collect_live_price: quote error for {symbol}: {e}", "WARNING", "BacktestEngine")
        return None
    if not isinstance(q, dict):
        log(f"⚠️ collect_live_price: quote not a dict for {symbol}", "WARNING", "BacktestEngine")
        return None

    delay = _quote_delay_seconds(q)
    if delay is None or delay > max_delay_sec:
        msg = "unknown freshness" if delay is None else f"delayed by {delay:.2f}s (> {max_delay_sec}s)"
        log(f"⚠️ collect_live_price: skipping {symbol} due to {msg}", "WARNING", "BacktestEngine")
        return None

    def _f(x):
        try:
            return float(x) if x is not None else None
        except Exception:
            return None

    last = _f(q.get("last"))
    if last is None:
        bid, ask = _f(q.get("bid")), _f(q.get("ask"))
        if bid is not None and ask is not None:
            last = (bid + ask) / 2.0
    if last is None:
        log(f"⚠️ collect_live_price: no usable price for {symbol}", "WARNING", "BacktestEngine")
        return None

    live_prices[symbol] = round(float(last), 6)
    atomic_save_json(live_prices, LIVE_PRICES_PATH)
    return float(last)

# --------------------------------------------------------------------------------------
# IBKR broker: safer placement, idempotent ensure-limit, cooldowns
# --------------------------------------------------------------------------------------

def _place_ibkr_market_order(symbol: str, qty: int, action: str, client_id: int, order_ref: Optional[str] = None) -> Dict[str, Any]:
    try:
        from ib_insync import IB, Stock, MarketOrder
    except Exception as e:
        return {"ok": False, "orderId": None, "status": "failed", "error": f"ib_insync not available: {e}"}

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        order = MarketOrder(action.upper(), int(qty))
        order.orderRef = order_ref or f"engine-{symbol}-{action.upper()}"
        try:
            order.outsideRth = True
        except Exception:
            pass

        trade = ib.placeOrder(contract, order)
        orderId = getattr(trade.order, "orderId", None)
        log(f"🚀 IBKR {action} {qty}x {symbol} @ MKT (orderId={orderId}, ref={order.orderRef}, clientId={client_id})", "INFO", "IBKR")
        return {"ok": True, "orderId": orderId, "orderRef": order.orderRef, "status": "submitted", "error": None}
    except Exception as e:
        import traceback as _tb
        log(f"❌ IBKR market order error: {e}\n{_tb.format_exc()}", "ERROR", "IBKR")
        return {"ok": False, "orderId": None, "status": "failed", "error": str(e)}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _place_ibkr_limit_order(symbol: str, qty: int, action: str, limit_price: float, client_id: int, order_ref: Optional[str] = None) -> Dict[str, Any]:
    try:
        from ib_insync import IB, Stock, LimitOrder
    except Exception as e:
        return {"ok": False, "orderId": None, "status": "failed", "error": f"ib_insync not available: {e}"}

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        order = LimitOrder(action.upper(), int(qty), float(limit_price))
        order.orderRef = order_ref or f"engine-{symbol}-{action.upper()}"
        try:
            order.outsideRth = True
        except Exception:
            pass

        trade = ib.placeOrder(contract, order)
        orderId = getattr(trade.order, "orderId", None)
        log(f"🚀 IBKR {action} {qty}x {symbol} @ {limit_price:.4f} (orderId={orderId}, ref={order.orderRef}, clientId={client_id})", "INFO", "IBKR")
        return {"ok": True, "orderId": orderId, "orderRef": order.orderRef, "status": "submitted", "error": None}
    except Exception as e:
        import traceback as _tb
        log(f"❌ IBKR order error: {e}\n{_tb.format_exc()}", "ERROR", "IBKR")
        return {"ok": False, "orderId": None, "status": "failed", "error": str(e)}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

def _find_trade_by_id_or_ref(ib, order_id: Optional[int], order_ref: Optional[str]):
    """Return (trade, status_str) if found; else (None, None)."""
    try:
        for tr in ib.trades():
            oid = getattr(tr.order, "orderId", None)
            ref = getattr(tr.order, "orderRef", None)
            if (order_id is not None and oid == order_id) or (order_ref and ref == order_ref):
                st = (getattr(tr.orderStatus, "status", "") or "").lower()
                return tr, st
    except Exception:
        pass
    return None, None

def _ensure_limit_order(symbol: str, action: str, qty: int, desired_limit: float, client_id: int, order_id: Optional[int], order_ref: Optional[str]) -> Dict[str, Any]:
    """
    Idempotent: if a working LMT at ~desired_limit exists -> keep; else place a new LMT.
    Returns dict with ok, orderId, orderRef, status, error.
    """
    try:
        from ib_insync import IB, Stock, LimitOrder
    except Exception as e:
        return {"ok": False, "orderId": order_id, "orderRef": order_ref, "status": "failed", "error": f"ib_insync not available: {e}"}

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)

        tr, st = _find_trade_by_id_or_ref(ib, order_id, order_ref)

        # If a working order already near the desired limit exists, keep it.
        if tr and st in ("presubmitted", "submitted", "pendingsubmit", "pendingcancel", "inactive"):
            try:
                existing_lim = float(getattr(tr.order, "lmtPrice", desired_limit))
                if abs(existing_lim - desired_limit) <= max(0.005, desired_limit * 0.0005):  # <= 0.05% or 0.5c
                    return {"ok": True, "orderId": getattr(tr.order, "orderId", order_id), "orderRef": getattr(tr.order, "orderRef", order_ref), "status": st, "error": None}
            except Exception:
                pass
            # Need a significant price change → try to cancel before replacing
            try:
                ib.cancelOrder(tr.order)
                ib.sleep(0.2)
            except Exception as e:
                import traceback as _tb
                log(f"⚠️ Cancel attempt failed for {symbol} ({action}): {e}\n{_tb.format_exc()}", "WARNING", "IBKR")

        # (Re)place a fresh LMT at desired_limit
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        new = LimitOrder(action.upper(), int(qty), float(desired_limit))
        new.orderRef = order_ref or f"engine-{symbol}-{action.upper()}"
        try:
            new.outsideRth = True
        except Exception:
            pass
        ntr = ib.placeOrder(contract, new)
        nid = getattr(ntr.order, "orderId", None)
        nref = getattr(ntr.order, "orderRef", new.orderRef)
        log(f"🔁 ensure LMT {action} {qty}x {symbol} @ {desired_limit:.4f} (orderId={nid}, ref={nref})", "INFO", "IBKR")
        return {"ok": True, "orderId": nid, "orderRef": nref, "status": "submitted", "error": None}

    except Exception as e:
        import traceback as _tb
        log(f"❌ IBKR ensure_limit error for {symbol} {action}: {e}\n{_tb.format_exc()}", "ERROR", "IBKR")
        return {"ok": False, "orderId": order_id, "orderRef": order_ref, "status": "failed", "error": str(e)}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

def _cooldown_ok(trade: Dict[str, Any], cooldown_sec: int = 20, field: str = "broker_retry") -> bool:
    """Return True if enough time passed since the last attempt; also updates 'tries' count."""
    br = trade.setdefault(field, {"last": None, "tries": 0})
    last = br.get("last")
    now = datetime.now(LOCAL_TZ)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = LOCAL_TZ.localize(last_dt)
            if (now - last_dt).total_seconds() < cooldown_sec:
                return False
        except Exception:
            pass
    br["last"] = now.isoformat()
    br["tries"] = int(br.get("tries", 0)) + 1
    trade[field] = br
    return True

# ---- Legacy helpers (kept; used sparingly) ----
def _best_bid_ask(symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    q = None
    try:
        q = get_polygon_quote(symbol)
    except Exception as e:
        log(f"⚠️ best_bid_ask quote err for {symbol}: {e}", "WARNING", "BacktestEngine")
    if isinstance(q, dict):
        return q.get("bid"), q.get("ask"), q.get("last")
    return None, None, None

def _cancel_and_replace_limit(order_id: int, symbol: str, action: str, new_limit: float, qty: int, client_id: int) -> Optional[int]:
    try:
        from ib_insync import IB, Stock, LimitOrder
    except Exception as e:
        log(f"❌ IBKR modify not available: {e}", "ERROR", "IBKR")
        return None
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        # cancel old
        for tr in ib.trades():
            if getattr(tr.order, "orderId", None) == order_id:
                ib.cancelOrder(tr.order)
                ib.sleep(0.2)
                break
        # place replacement
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        new_order = LimitOrder(action.upper(), int(qty), float(new_limit))
        new_order.orderRef = f"engine-{symbol}-{action.upper()}"
        try:
            new_order.outsideRth = True
        except Exception:
            pass
        trade = ib.placeOrder(contract, new_order)
        new_id = getattr(trade.order, "orderId", None)
        log(f"🔁 IBKR {action} replace -> LMT {qty}x {symbol} @ {new_limit:.4f} (old={order_id}, new={new_id})", "INFO", "IBKR")
        return new_id
    except Exception as e:
        log(f"❌ IBKR cancel/replace error: {e}", "ERROR", "IBKR")
        return None
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

def _cancel_and_replace_market(order_id: int, symbol: str, action: str, qty: int, client_id: int) -> Optional[int]:
    try:
        from ib_insync import IB, Stock, MarketOrder
    except Exception as e:
        log(f"❌ IBKR market replace not available: {e}", "ERROR", "IBKR")
        return None
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=client_id)
        for tr in ib.trades():
            if getattr(tr.order, "orderId", None) == order_id:
                ib.cancelOrder(tr.order)
                ib.sleep(0.2)
                break
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        mkt = MarketOrder(action.upper(), int(qty))
        mkt.orderRef = f"engine-{symbol}-{action.upper()}"
        try:
            mkt.outsideRth = True
        except Exception:
            pass
        trade = ib.placeOrder(contract, mkt)
        new_id = getattr(trade.order, "orderId", None)
        log(f"🏁 IBKR {action} replace -> MKT {qty}x {symbol} (old={order_id}, new={new_id})", "INFO", "IBKR")
        return new_id
    except Exception as e:
        log(f"❌ IBKR market replace error: {e}", "ERROR", "IBKR")
        return None
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

# --------------------------------------------------------------------------------------
# Gainers list
# --------------------------------------------------------------------------------------
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

# --------------------------------------------------------------------------------------
# Exit rules (parameterized)
# --------------------------------------------------------------------------------------
@dataclass
class ExitDecision:
    should_exit: bool
    reason: str
    intended_price: Optional[float]

def _negative_exit_block_secs() -> int:
    lp = load_json(LIVE_PARAMS_PATH)
    return int(lp.get("negative_exit_block_secs", NEGATIVE_EXIT_BLOCK_SECS_DEFAULT))

def evaluate_exit(trade: Dict[str, Any], exit_params: Dict[str, Any], live_price: float) -> ExitDecision:
    entry = trade.get("entry_price") or trade.get("intended_entry_price")
    if not entry or not live_price:
        return ExitDecision(False, "", None)

    tp = exit_params.get("take_profit_pct", 0.6) / 100.0
    sl = exit_params.get("stop_loss_pct", 0.6) / 100.0
    max_hold = int(exit_params.get("max_hold_secs", 1200))
    early_profit = exit_params.get("early_profit_pct")
    early_loss   = exit_params.get("early_loss_pct")

    # hold time
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

    # block any negative-PnL exits until the configured window elapses
    if hold_secs < _negative_exit_block_secs() and live_price < entry:
        # allow only positive outcomes within the block window
        gain = (live_price - entry) / entry
        if tp and gain >= tp:
            return ExitDecision(True, "take_profit", live_price)
        return ExitDecision(False, "", None)

    gain = (live_price - entry) / entry
    if sl and gain <= -sl:              return ExitDecision(True, "stop_loss",   live_price)
    if tp and gain >= tp:               return ExitDecision(True, "take_profit", live_price)
    if max_hold and hold_secs >= max_hold:
                                        return ExitDecision(True, "time_exit",   live_price)
    if early_profit:
        ep = early_profit/100.0
        if 0 < gain < ep and hold_secs >= 60:
                                        return ExitDecision(True, "early_profit_lock", live_price)
    if early_loss:
        el = early_loss/100.0
        if -el < gain < 0 and hold_secs >= 60:
                                        return ExitDecision(True, "early_loss_cut", live_price)
    return ExitDecision(False, "", None)

# --------------------------------------------------------------------------------------
# Inline sync for fills: pending_* -> open/closed
# --------------------------------------------------------------------------------------
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
            shares = getattr(execution, "shares", 0) or 0
            price = getattr(execution, "price", 0.0) or 0.0
            ex_time = getattr(execution, "time", "")

            if oid is None:
                continue

            d = by_order.setdefault(oid, {"cumQty": 0, "value": 0.0, "side": side, "lastTime": ex_time, "symbol": getattr(contract, "symbol", None)})
            d["cumQty"] += int(shares)
            d["value"]  += float(price) * int(shares)
            try:
                if str(ex_time) > str(d["lastTime"]):
                    d["lastTime"] = ex_time
            except Exception:
                pass

        # Map orderRef -> orderId as a fallback
        by_ref: Dict[str, int] = {}
        try:
            for tr in ib.trades():
                rid = getattr(tr.order, "orderId", None)
                rref = getattr(tr.order, "orderRef", None)
                if rref and rid is not None:
                    by_ref[rref] = rid
        except Exception:
            pass

        positions = {getattr(p.contract, "symbol", None): p for p in ib.positions()}

        changed = False
        prom_buys = prom_sells = 0
        for _, bucket in trades_book.items():
            for t in bucket.get("trades", []):
                if t.get("status") == "pending_buy":
                    orderId = _coerce_int(t.get("broker_order", {}).get("orderId"))
                    if orderId is None:
                        ref = t.get("broker_order", {}).get("orderRef")
                        if ref:
                            orderId = by_ref.get(ref)
                            if orderId is not None:
                                t["broker_order"]["orderId"] = orderId
                    shares  = int(t.get("shares", 0))
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
                        if pos and abs(int(getattr(pos, "position", 0))) >= shares:
                            t["status"] = "open"
                            if getattr(pos, "avgCost", None):
                                t["entry_price"] = float(pos.avgCost); t["entry_price_source"] = "ibkr_position"
                            prom_buys += 1; changed = True

                elif t.get("status") == "pending_sell":
                    orderId = _coerce_int(t.get("broker_order_sell", {}).get("orderId"))
                    if orderId is None:
                        ref = t.get("broker_order_sell", {}).get("orderRef")
                        if ref:
                            orderId = by_ref.get(ref)
                            if orderId is not None:
                                t["broker_order_sell"]["orderId"] = orderId
                    shares  = int(t.get("shares", 0))
                    rec = by_order.get(orderId) if orderId is not None else None
                    if rec and rec.get("side") == "SLD" and rec.get("cumQty", 0) >= shares:
                        avg = (rec["value"] / rec["cumQty"]) if rec["cumQty"] else None
                        exit_price = float(avg) if avg else None
                        t["exit_time"] = datetime.now(LOCAL_TZ).isoformat()
                        if exit_price is not None:
                            t["exit_price"] = exit_price
                            ep = t.get("entry_price") or t.get("intended_entry_price") or 0.0
                            sh = int(t.get("shares", 0))
                            t["pnl"] = round((exit_price - ep) * sh, 2) if ep else 0.0
                            t["pnl_pct"] = ((exit_price - ep) / ep) * 100 if ep else 0.0
                        t["status"] = "closed"
                        prom_sells += 1; changed = True
                    else:
                        pos = positions.get(t.get("symbol"))
                        qty = abs(int(getattr(pos, "position", 0))) if pos else 0
                        if qty == 0:
                            t["exit_time"] = datetime.now(LOCAL_TZ).isoformat()
                            lp = get_live_price(t.get("symbol"))
                            if lp:
                                t["exit_price"] = float(lp)
                                ep = t.get("entry_price") or t.get("intended_entry_price") or 0.0
                                sh = int(t.get("shares", 0))
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

# --------------------------------------------------------------------------------------
# Pending Watchdog (sticky sells, buy unclog, cooldown)
# --------------------------------------------------------------------------------------
def _read_pending_policy():
    cfg = load_json(LIVE_PARAMS_PATH).get("pending_policy", {})
    def section(side):
        s = cfg.get(side, {})
        return {
            "reprice_after_s": int(s.get("reprice_after_s", 20 if side=="buy" else 15)),
            "convert_to_market_after_s": int(s.get("convert_to_market_after_s", 60 if side=="buy" else 45)),
            "reprice_offset_bps": float(s.get("reprice_offset_bps", 5.0)),
            "max_slippage_bps": float(s.get("max_slippage_bps", 20.0)),
        }
    max_pending_total = int(cfg.get("max_pending_total", MAX_PENDING_TOTAL_DEFAULT))
    return bool(cfg.get("enabled", True)), section("buy"), section("sell"), max_pending_total

def _pending_age_secs(trade: Dict[str, Any]) -> float:
    try:
        t0s = trade.get("datetime")
        if not t0s: return 1e9
        t0 = datetime.fromisoformat(t0s)
        if t0.tzinfo is None:
            t0 = LOCAL_TZ.localize(t0)
        return (datetime.now(LOCAL_TZ) - t0).total_seconds()
    except Exception:
        return 1e9

def _sell_floor(trade: Dict[str, Any]) -> float:
    # Never execute a SELL below this
    return float(trade.get("intended_exit_price") or trade.get("entry_price") or 0.0)

def _buy_ceiling(trade: Dict[str, Any]) -> float:
    # Never execute a BUY above this
    return float(trade.get("intended_entry_price") or 0.0)

def manage_pending_orders():
    enabled, buy_pol, sell_pol, max_pending_total = _read_pending_policy()
    if not enabled:
        return
    book = load_json(LIVE_TRADES_PATH)
    if not book:
        return

    # Collect pendings
    pending_buys, pending_sells = [], []
    for _, bucket in book.items():
        for t in bucket.get("trades", []):
            if t.get("status") == "pending_buy":
                pending_buys.append((t, _pending_age_secs(t)))
            elif t.get("status") == "pending_sell":
                pending_sells.append((t, _pending_age_secs(t)))

    pending_buys.sort(key=lambda x: x[1], reverse=True)   # oldest first
    pending_sells.sort(key=lambda x: x[1], reverse=True)

    changed = False

    # --- BUY side: allow reprice/abort to avoid clogging ---
    for t, age in pending_buys:
        shares = int(t.get("shares", 0)) or 0
        if shares <= 0:
            continue
        intended = float(t.get("intended_entry_price") or 0.0)
        bo = t.get("broker_order", {}) or {}
        order_id = bo.get("orderId")
        client_id = IB_CLIENT_ID_BUY
        ceiling = _buy_ceiling(t)
        if order_id is None:
            continue

        bid, ask, last = _best_bid_ask(t.get("symbol"))
        off = buy_pol["reprice_offset_bps"] / 10000.0
        lim = buy_pol["max_slippage_bps"] / 10000.0

        esc = t.setdefault("pending_escalation", {"stage": "none", "history": []})
        stage = esc.get("stage", "none")

        # Debounce attempts
        if not _cooldown_ok(t, cooldown_sec=20, field="buy_retry"):
            continue

        # A) Reprice once toward best ask (but never above ceiling)
        if stage == "none" and age >= buy_pol["reprice_after_s"]:
            ref = ask or last or intended
            new_limit = min(intended * (1 + lim), (ref or intended) * (1 + off))
            new_limit = min(new_limit, ceiling) if ceiling > 0 else new_limit
            if new_limit <= 0 or (ceiling > 0 and new_limit > ceiling):
                esc["stage"] = "aborted_above_ceiling"
                esc["history"].append({"ts": datetime.now(LOCAL_TZ).isoformat(), "action": "abort_reprice_above_ceiling"})
                t["status"] = "aborted_buy"
                changed = True
            else:
                new_id = _cancel_and_replace_limit(order_id, t["symbol"], "BUY", new_limit, shares, client_id)
                if new_id is not None:
                    bo["orderId"] = new_id; bo["price"] = float(new_limit); bo["type"] = "LMT"
                    t["broker_order"] = bo
                    esc["stage"] = "repriced"
                    esc["history"].append({"ts": datetime.now(LOCAL_TZ).isoformat(), "action": "repriced", "price": float(new_limit)})
                    changed = True

        # B) Convert to market only if <= ceiling; else abort to unclog
        if age >= buy_pol["convert_to_market_after_s"] and stage in ("none","repriced"):
            ok_market = False
            for price_ref in (ask, last):
                if price_ref and (ceiling <= 0 or price_ref <= ceiling):
                    ok_market = True; break
            if ok_market:
                new_id = _cancel_and_replace_market(order_id, t["symbol"], "BUY", shares, client_id)
                if new_id is not None:
                    bo["orderId"] = new_id; bo["type"] = "MKT"; bo["price"] = None
                    t["broker_order"] = bo
                    esc["stage"] = "market"
                    esc["history"].append({"ts": datetime.now(LOCAL_TZ).isoformat(), "action": "convert_to_market"})
                    changed = True
            else:
                esc["stage"] = "aborted_above_ceiling"
                esc["history"].append({"ts": datetime.now(LOCAL_TZ).isoformat(), "action": "abort_market_above_ceiling"})
                t["status"] = "aborted_buy"
                changed = True

    # --- SELL side: STICKY LIMIT at floor (never market) with cooldown + max tries ---
    MAX_SELL_ENSURE_TRIES = 12  # ~4–6 min depending on cooldown

    if pending_sells:
        for t, age in pending_sells:
            shares = int(t.get("shares", 0)) or 0
            if shares <= 0:
                continue

            symbol = t.get("symbol")
            entry = float(t.get("entry_price") or t.get("intended_entry_price") or 0.0)
            bos = t.get("broker_order_sell", {}) or {}
            oid = bos.get("orderId")
            oref = bos.get("orderRef") or f"engine-{symbol}-SELL"

            # Fresh price required to apply the market rule
            price = get_live_price(symbol, max_delay_sec=10.0)
            if price is not None and entry > 0:
                # Rule: PnL >= 0 → MKT; or PnL < 0 and age >= 30 min → MKT
                neg_block = _negative_exit_block_secs()
                if (price >= entry) or (price < entry and age >= neg_block):
                    if not _cooldown_ok(t, cooldown_sec=10, field="sell_retry"):
                        continue
                    new_id = _cancel_and_replace_market(oid, symbol, "SELL", shares, IB_CLIENT_ID_SELL)
                    if new_id is not None:
                        bos.update({"orderId": new_id, "orderRef": oref, "type": "MKT", "price": None, "clientId": IB_CLIENT_ID_SELL})
                        t["broker_order_sell"] = bos
                        t.setdefault("pending_escalation", {}).update({"stage": "convert_to_market_rule"})
                        changed = True
                    # whether success or not, skip to next pending SELL
                    continue

            # Otherwise keep a sticky LMT at floor
            floor = _sell_floor(t)
            if not _cooldown_ok(t, cooldown_sec=20, field="sell_retry"):
                continue
            if t.get("sell_retry", {}).get("tries", 0) > MAX_SELL_ENSURE_TRIES:
                t.setdefault("pending_escalation", {}).update({"stage": "sell_ensure_gave_up"})
                continue

            resp = _ensure_limit_order(symbol, "SELL", shares, floor, IB_CLIENT_ID_SELL, oid, oref)
            if resp.get("ok"):
                bos.update({"orderId": resp.get("orderId"), "orderRef": resp.get("orderRef"), "type": "LMT", "price": float(floor), "clientId": IB_CLIENT_ID_SELL})
                t["broker_order_sell"] = bos
                t["status"] = "pending_sell"
                t.setdefault("pending_escalation", {}).update({"stage": "resting_limit"})
                changed = True
            else:
                t.setdefault("pending_escalation", {}).update({"stage": "sell_ensure_error"})
                changed = True

# --------------------------------------------------------------------------------------
# BUY logic
# --------------------------------------------------------------------------------------
@fail_safe(module="BacktestEngine", critical=True)
def generate_buy_orders():
    if not market_is_open():
        log("Market closed.", "INFO", "BacktestEngine")
        return
    if datetime.now(LOCAL_TZ).time() >= dtime(16, 0):
        log("🛑 Market closed at 16:00, skipping BUY generation.", "INFO", "BacktestEngine")
        return

    live_params = load_json(LIVE_PARAMS_PATH)
    control = load_json(CONTROL_PATH)
    live_trades = load_json(LIVE_TRADES_PATH)

    # Pause buys if pending pressure too high
    max_pending_total = int(live_params.get("pending_policy", {}).get("max_pending_total", MAX_PENDING_TOTAL_DEFAULT))
    current_pending = sum(1 for _, b in live_trades.items() for t in b.get("trades", []) if t.get("status") == "pending_buy")
    if current_pending >= max_pending_total:
        log(f"⛔ Too many pending buys ({current_pending} >= {max_pending_total}), skipping new buys.", "INFO", "BacktestEngine")
        return

    min_confidence = float(live_params.get("min_confidence", MIN_CONFIDENCE))
    alloc = live_params.get("capital_allocation", {})
    base_pct = float(alloc.get("base_pct", 0.1))
    bonus_pct = float(alloc.get("bonus_pct", 0.2))
    max_allocation_pct = float(alloc.get("max_allocation_pct", 0.4))
    position_cap = float(alloc.get("position_cap", CAPITAL_LIMIT))

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

        # Optional: AI signal filter (best-effort)
        try:
            sig = get_strategy_signals(symbol)
            conf = float(sig.get("confidence", 1.0))
        except Exception:
            conf = 1.0
        if conf < min_confidence:
            continue

        price = get_live_price(symbol, max_delay_sec=10.0)
        if price is None:
            continue

        # update live_prices flat file
        collect_live_price(symbol, max_delay_sec=10.0)

        # capital allocation
        alloc_pct = min(base_pct + (bonus_pct if conf >= 0.8 else 0.0), max_allocation_pct)
        dollars = min(position_cap, CAPITAL_LIMIT * alloc_pct)
        shares = max(1, int(dollars // price))
        if shares <= 0:
            continue

        intended_entry_price = float(price)

        # Place BUY LMT at intended price (ceiling)
        order_resp = _place_ibkr_limit_order(
            symbol, shares, "BUY", intended_entry_price, client_id=IB_CLIENT_ID_BUY, order_ref=f"engine-{symbol}-BUY"
        )

        trade = {
            "symbol": symbol,
            "datetime": datetime.now(LOCAL_TZ).isoformat(),
            "status": "pending_buy" if order_resp.get("ok") else "error_buy",
            "shares": shares,
            "intended_entry_price": intended_entry_price,
            "broker_order": {
                "action": "BUY",
                "type": "LMT",
                "price": intended_entry_price,
                "orderId": order_resp.get("orderId"),
                "orderRef": order_resp.get("orderRef"),
                "status": order_resp.get("status"),
                "error": order_resp.get("error"),
                "clientId": IB_CLIENT_ID_BUY,
            },
            "confidence": conf,
        }
        today_bucket["trades"].append(trade)

        # persist
        write_json_file(LIVE_TRADES_PATH, live_trades)

        # append order log (best-effort)
        orders = load_json(BUY_ORDERS_PATH) or []
        orders.append({
            "ts": datetime.now(LOCAL_TZ).isoformat(),
            "symbol": symbol,
            "action": "BUY",
            "price": intended_entry_price,
            "shares": shares,
            "orderId": order_resp.get("orderId"),
            "orderRef": order_resp.get("orderRef"),
            "status": order_resp.get("status"),
        })
        write_json_file(BUY_ORDERS_PATH, orders)

# --------------------------------------------------------------------------------------
# SELL logic
# --------------------------------------------------------------------------------------
@fail_safe(module="BacktestEngine", critical=True)
def generate_sell_orders():
    # We allow generating sells even if market_is_open() is False because
    # the resting orders will be handled by IBKR; but we'll still skip if no fresh prices.
    exit_params = load_json(EXIT_PARAMS_PATH)
    live_trades = load_json(LIVE_TRADES_PATH)
    changed = False

    for _, bucket in live_trades.items():
        for trade in bucket.get("trades", []):
            if trade.get("status") != "open":
                continue
            symbol = trade.get("symbol")
            if not symbol:
                continue

            # Need a *fresh* price
            price = get_live_price(symbol, max_delay_sec=10.0)
            if price is None:
                # Still update live price file if possible (best-effort)
                collect_live_price(symbol, max_delay_sec=10.0)
                continue

            # Update live price file on successful read
            collect_live_price(symbol, max_delay_sec=10.0)

            # Compute PnL and hold time
            entry = float(trade.get("entry_price") or trade.get("intended_entry_price") or price)
            shares = int(trade.get("shares", 0)) or 0
            if shares <= 0:
                continue

            # hold time
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

            # --- New rule: market exits ---
            # 1) If PnL >= 0 → SELL @ MKT
            # 2) If PnL < 0 and age >= 30 min → SELL @ MKT
            negative_block = _negative_exit_block_secs()
            if (price >= entry) or (price < entry and hold_secs >= negative_block):
                reason = "market_positive" if price >= entry else "market_after_30m_negative"
                order_resp = _place_ibkr_market_order(
                    symbol,
                    shares,
                    "SELL",
                    client_id=IB_CLIENT_ID_SELL,
                    order_ref=f"engine-{symbol}-SELL"
                )
                trade["intended_exit_price"] = float(price)  # informational
                trade["exit_reason"] = reason
                trade["status"] = "pending_sell" if order_resp.get("ok") else "error_sell"
                trade["broker_order_sell"] = {
                    "action": "SELL",
                    "type": "MKT",
                    "price": None,
                    "orderId": order_resp.get("orderId"),
                    "orderRef": order_resp.get("orderRef"),
                    "status": order_resp.get("status"),
                    "error": order_resp.get("error"),
                    "clientId": IB_CLIENT_ID_SELL,
                }
                if "exit_armed_at" not in trade:
                    trade["exit_armed_at"] = datetime.now(LOCAL_TZ).isoformat()
                changed = True
                continue

            # --- Otherwise fall back to existing rule-set (evaluates TP/SL/etc.) ---
            decision = evaluate_exit(trade, exit_params, price)
            if not decision.should_exit:
                continue

            intended_exit_price = float(decision.intended_price or price)
            # Keep a non-breaching floor for the limit case
            floor = max(intended_exit_price, entry)

            if "exit_armed_at" not in trade:
                trade["exit_armed_at"] = datetime.now(LOCAL_TZ).isoformat()

            order_resp = _place_ibkr_limit_order(
                symbol,
                shares,
                "SELL",
                floor,
                client_id=IB_CLIENT_ID_SELL,
                order_ref=f"engine-{symbol}-SELL"
            )

            trade["intended_exit_price"] = floor
            trade["exit_reason"] = decision.reason
            trade["status"] = "pending_sell" if order_resp.get("ok") else "error_sell"
            trade["broker_order_sell"] = {
                "action": "SELL",
                "type": "LMT",
                "price": float(floor),
                "orderId": order_resp.get("orderId"),
                "orderRef": order_resp.get("orderRef"),
                "status": order_resp.get("status"),
                "error": order_resp.get("error"),
                "clientId": IB_CLIENT_ID_SELL,
            }
            changed = True

    if changed:
        write_json_file(LIVE_TRADES_PATH, live_trades)
        # Append order log (best-effort)
        orders = load_json(SELL_ORDERS_PATH) or []
        for _, bucket in live_trades.items():
            for t in bucket.get("trades", []):
                if t.get("status") == "pending_sell":
                    orders.append({
                        "ts": datetime.now(LOCAL_TZ).isoformat(),
                        "symbol": t.get("symbol"),
                        "action": "SELL",
                        "price": float(t.get("intended_exit_price") or 0.0) if t.get("broker_order_sell", {}).get("type") == "LMT" else None,
                        "shares": int(t.get("shares") or 0),
                        "orderId": t.get("broker_order_sell", {}).get("orderId"),
                        "orderRef": t.get("broker_order_sell", {}).get("orderRef"),
                        "status": t.get("broker_order_sell", {}).get("status"),
                        "type": t.get("broker_order_sell", {}).get("type"),
                    })
        write_json_file(SELL_ORDERS_PATH, orders)


# --------------------------------------------------------------------------------------
# Main run loop
# --------------------------------------------------------------------------------------
def run_loop():
    while True:
        try:
            # Update fills first (promote pending->open/closed)
            sync_broker_fills()
            # Manage pendings (sticky sells, unclog buys)
            manage_pending_orders()
            # Arm exits as needed
            generate_sell_orders()
            # Create new entries
            generate_buy_orders()
        except Exception as e:
            log(f"⚠️ Error in BacktestEngine: {e}", "ERROR", "BacktestEngine")
        finally:
            log(f"Sleeping {LOOP_INTERVAL}s before next cycle.", "INFO", "BacktestEngine")
            time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run_loop()
