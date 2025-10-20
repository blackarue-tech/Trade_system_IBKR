import os, json, tempfile, shutil
from datetime import datetime
from brokers.ibkr_state import load_state

LIVE_TRADES = os.path.join("exports", "live_trades.json")

def _load_live():
    if not os.path.exists(LIVE_TRADES):
        return {}
    with open(LIVE_TRADES, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_live(d):
    os.makedirs(os.path.dirname(LIVE_TRADES), exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        json.dump(d, tmp, indent=2)
        tmp_path = tmp.name
    shutil.move(tmp_path, LIVE_TRADES)

def reconcile_once():
    state = load_state()
    live = _load_live()
    day = datetime.now().strftime("%Y-%m-%d")
    live.setdefault(day, {"trades": []})

    trades = live[day]["trades"]

    # Helper lookups
    def find_open_trade_for_symbol(sym):
        # last opened, still open
        for t in reversed(trades):
            if t.get("symbol") == sym and t.get("status") == "open":
                return t
        return None

    def find_trade_by_orderid(oid):
        for t in trades:
            if t.get("orderId") == oid:
                return t
        return None

    # 1) Apply fills (this captures instant fills even when no open orders are visible)
    for f in state.get("fills", []):
        oid = f.get("orderId")
        sym = f.get("symbol")
        side = f.get("side")
        qty  = f.get("shares")
        px   = f.get("price")
        tstr = f.get("time")  # 'YYYYMMDD  HH:MM:SS <TZ>'

        if side == "BOT":
            # If we already have this order recorded, update it; else create new open trade
            tr = find_trade_by_orderid(oid) or find_open_trade_for_symbol(sym)
            if tr is None:
                trades.append({
                    "symbol": sym,
                    "orderId": oid,
                    "status": "open",
                    "entry_price": px,
                    "shares": qty,
                    "datetime": datetime.now().isoformat(),
                    "entry_fill_time": tstr
                })
            else:
                tr["status"] = "open"
                tr["entry_price"] = tr.get("entry_price", px)
                tr["shares"] = max(tr.get("shares", 0), qty)
                tr["entry_fill_time"] = tstr

        elif side == "SLD":
            tr = find_open_trade_for_symbol(sym) or find_trade_by_orderid(oid)
            if tr is not None and tr.get("status") == "open":
                tr["exit_price"] = px
                tr["exit_time"]  = tstr
                # simple PnL; adjust if you want commissions
                if "entry_price" in tr and "shares" in tr:
                    tr["pnl"] = round((tr["exit_price"] - tr["entry_price"]) * tr["shares"], 2)
                    tr["pnl_pct"] = (tr["exit_price"] - tr["entry_price"]) / tr["entry_price"] * 100.0
                tr["status"] = "closed"

    # 2) Backfill opens from positions (in case we missed fills earlier)
    current_syms = {p["symbol"] for p in state.get("positions", []) if abs(p.get("position", 0)) > 0}
    for p in state.get("positions", []):
        sym = p["symbol"]; pos = p["position"]; avg = p.get("avgCost")
        if abs(pos) == 0:
            continue
        if not find_open_trade_for_symbol(sym):
            trades.append({
                "symbol": sym,
                "status": "open",
                "entry_price": avg,
                "shares": pos,
                "datetime": datetime.now().isoformat(),
                "source": "position_backfill"
            })

    _save_live(live)
