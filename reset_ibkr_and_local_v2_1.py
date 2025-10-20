"""
reset_ibkr_and_local_v2_1.py

Adds an active "chase" close loop so paper positions that sometimes refuse to fill
quickly will be re-issued as MARKET until they actually go to zero (excluding
compliance-restricted symbols like BJDX/Rule 144).

Key features (superset of v2):
- SMART routing by conId when available (avoids 10311 direct-routing warnings)
- Detect compliance cancels (error 201 / Rule 144) -> exports/restricted_symbols.json
- --chase N  : actively poll positions & order statuses for up to N seconds, re-issuing orders
- --wait-zero keeps working; --chase is usually better on paper because it re-places
- --then-wipe and --wipe-even-if-restricted same as before

Usage
-----
python reset_ibkr_and_local_v2_1.py --flatten-ibkr --confirm YES --chase 180 --then-wipe
"""
from __future__ import annotations
import os, json, time, shutil, re
from datetime import datetime
from typing import List, Dict, Any, Tuple, Set

try:
    from ib_insync import IB, MarketOrder, Contract
    HAVE_IB = True
except Exception:
    HAVE_IB = False

LOCAL_FILES: Dict[str, Any] = {    
    "exports/ibkr_orders_log.json": [],
    "exports/ibkr_orders_status.json": [],
    "exports/ibkr_positions.json": {},
    "exports/sync_audit.json": {},
    "exports/exit_failures.json": [],
    "exports/recovery_tracker.json": {},
    "exports/last_exit_tracker.json": {},   
    "exports/polygon_quotes.json": {},
}
RESTRICTED_PATH = "exports/restricted_symbols.json"

# ---------- helpers ----------

def ensure_dir(p: str):
    os.makedirs(os.path.dirname(p), exist_ok=True)


def atomic_write_json(path: str, obj: Any):
    ensure_dir(path)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def backup_existing(paths: List[str]) -> str | None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join("exports", f"_backup_reset_{ts}")
    os.makedirs(backup_dir, exist_ok=True)
    moved = False
    for p in paths:
        if os.path.exists(p):
            try:
                dest = os.path.join(backup_dir, os.path.basename(p))
                shutil.move(p, dest)
                moved = True
            except Exception as e:
                print(f"⚠️ Failed to backup {p}: {e}")
    return backup_dir if moved else None


def wipe_local_state() -> str:
    paths = list(LOCAL_FILES.keys())
    backup_dir = backup_existing(paths)
    for p, default in LOCAL_FILES.items():
        try:
            atomic_write_json(p, default)
            print(f"🧹 reset -> {p}")
        except Exception as e:
            print(f"⚠️ failed to write {p}: {e}")
    return backup_dir or ""

# ---------- IB helpers ----------

def connect_ib(host: str, port: int, client_id: int) -> IB:
    if not HAVE_IB:
        raise RuntimeError("ib_insync not installed. pip install ib-insync")
    ib = IB()
    ib.connect(host, port, clientId=int(client_id), timeout=6)
    return ib


def is_paper_account(ib: IB) -> bool:
    try:
        accts = ib.managedAccounts() or []
        return all(str(a).upper().startswith("DU") for a in accts) and len(accts) > 0
    except Exception:
        return False


def smart_stock_contract_from_conId(conId: int, currency: str | None = None, primaryExchange: str | None = None) -> Contract:
    c = Contract()
    c.secType = 'STK'
    c.conId = int(conId)
    c.exchange = 'SMART'
    c.currency = currency or 'USD'
    if primaryExchange:
        c.primaryExchange = primaryExchange
    return c


def cancel_all_open_orders(ib: IB) -> int:
    open_trades = ib.openTrades()
    if not open_trades:
        print("✅ No open orders.")
        return 0
    print(f"🚫 Canceling {len(open_trades)} open orders...")
    for tr in open_trades:
        try:
            ib.cancelOrder(tr.order)
        except Exception as e:
            print(f"  ⚠️ cancel failed for orderId={getattr(tr.order, 'orderId', '?')}: {e}")
    ib.sleep(0.7)
    return len(open_trades)


def flatten_stock_positions(ib: IB, *, dry_run: bool = False) -> Tuple[int, Dict[str, str], list]:
    """Place MARKET orders to flatten stock positions.
    Returns (placed_count, restricted_map, placed_trade_objs)."""
    positions = ib.positions()
    stk_positions = [p for p in positions if (p.contract.secType or 'STK').upper() == 'STK' and int(p.position or 0) != 0]
    if not stk_positions:
        print("✅ No stock positions to flatten.")
        return 0, {}, []

    print(f"🧨 Flattening {len(stk_positions)} stock positions with MARKET orders...")
    placed = 0
    placed_trades = []

    for p in stk_positions:
        qty = int(p.position or 0)
        side = 'SELL' if qty > 0 else 'BUY'
        con = smart_stock_contract_from_conId(p.contract.conId, currency=p.contract.currency or 'USD', primaryExchange=getattr(p.contract, 'primaryExchange', None))
        try:
            ib.qualifyContracts(con)
        except Exception:
            con = p.contract
        print(f"  {side} {abs(qty)} {getattr(con, 'symbol', p.contract.symbol)} @ MKT")
        if dry_run:
            continue
        try:
            o = MarketOrder(side, abs(qty), tif='DAY', outsideRth=True)
            tr = ib.placeOrder(con, o)
            placed += 1
            placed_trades.append(tr)
            print(f"    → placed orderId={getattr(tr.order, 'orderId', '?')}")
        except Exception as e:
            print(f"    ⚠️ place failed: {e}")

    # compliance sweep after initial placements
    restricted: Dict[str, str] = {}
    if not dry_run and placed_trades:
        # let statuses/logs populate
        for _ in range(10):
            ib.reqOpenOrders(); ib.reqExecutions(); ib.sleep(0.25)
        pattern_201 = re.compile(r"(?:Error\s+201|Rule\s*144|micro-?cap|compliance)", re.I)
        for tr in ib.trades():
            try:
                status = getattr(tr.orderStatus, 'status', '') or ''
                sym = getattr(tr.contract, 'symbol', '') or ''
                adv = getattr(tr, 'advancedError', '') or ''
                logtxt = "\n".join([getattr(e, 'message', '') or '' for e in getattr(tr, 'log', [])])
                txt = f"{adv}\n{logtxt}"
                if status.lower() in ("cancelled", "inactive") and pattern_201.search(txt):
                    restricted[sym] = (adv or logtxt or "Compliance/Rule 144 restriction").strip()
            except Exception:
                pass

    return placed, restricted, placed_trades


def chase_to_zero(
    ib: IB,
    seconds: int,
    *,
    restricted_syms: Set[str] | None = None,
    verbose: bool = True,
) -> bool:
    """Actively try to force positions to zero for up to `seconds`.
    Re-places MARKET orders when there is no active working order for a non-zero position.
    Excludes compliance-restricted symbols.
    """
    restricted_syms = restricted_syms or set()
    deadline = time.monotonic() + max(0, int(seconds))
    # Track last action per symbol to avoid spam
    last_place_ts: Dict[str, float] = {}

    while True:
        still_open: list[tuple[str, int, int]] = []  # (sym, qty, conId)
        for p in ib.positions():
            try:
                if (p.contract.secType or 'STK').upper() != 'STK':
                    continue
                sym = p.contract.symbol
                if sym in restricted_syms:
                    continue
                qty = int(p.position or 0)
                if qty != 0:
                    still_open.append((sym, qty, int(p.contract.conId)))
            except Exception:
                continue

        if not still_open:
            return True

        now = time.monotonic()
        # Build working orders map by symbol -> side/status
        working = {}
        for tr in ib.openTrades():
            try:
                sym = tr.contract.symbol
                st = (getattr(tr.orderStatus, 'status', '') or '').lower()
                side = getattr(tr.order, 'action', '')
                working.setdefault(sym, []).append((side, st))
            except Exception:
                pass

        for sym, qty, conId in still_open:
            side_needed = 'SELL' if qty > 0 else 'BUY'
            # If we already have a working order on correct side, just continue
            has_working = any((side == side_needed and st in ("submitted", "pre-submitted", "pendingcancel", "pendingsubmit")) for side, st in working.get(sym, []))
            if has_working:
                continue
            # Throttle re-placement to at most once per 2s per symbol
            if now - last_place_ts.get(sym, 0) < 2.0:
                continue

            # Place/replace order
            try:
                con = smart_stock_contract_from_conId(conId)
                try:
                    ib.qualifyContracts(con)
                except Exception:
                    pass
                o = MarketOrder(side_needed, abs(qty), tif='DAY', outsideRth=True)
                tr = ib.placeOrder(con, o)
                last_place_ts[sym] = now
                if verbose:
                    print(f"  ↻ re-place {side_needed} {abs(qty)} {sym} @ MKT (orderId={getattr(tr.order, 'orderId', '?')})")
            except Exception as e:
                if verbose:
                    print(f"  ⚠️ re-place failed for {sym}: {e}")

        if time.monotonic() >= deadline:
            if verbose:
                rem = [(s, q) for s, q, _ in still_open]
                print(f"⏳ Still open after chase: {rem}")
            return False

        ib.reqOpenOrders(); ib.reqExecutions(); ib.sleep(0.5)

# ---------- CLI ----------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reset local state and/or flatten IBKR paper account with SMART routing, compliance handling and active chase")
    parser.add_argument("--wipe-local", action="store_true", help="Wipe/initialize local JSON state with backups")
    parser.add_argument("--flatten-ibkr", action="store_true", help="Cancel open orders and liquidate STOCK positions via SMART routing")
    parser.add_argument("--confirm", type=str, default="", help='Type YES to confirm placing market orders for flatten step')
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=777)
    parser.add_argument("--wait-zero", type=int, default=0, help="Legacy: wait for zero without re-placing orders")
    parser.add_argument("--chase", type=int, default=0, help="Actively re-place orders for up to N seconds until positions go to zero")
    parser.add_argument("--then-wipe", action="store_true", help="After successful flatten/wait/chase, wipe local files")
    parser.add_argument("--wipe-even-if-restricted", action="store_true", help="Allow local wipe even if some symbols are compliance-restricted")

    args = parser.parse_args()

    if not args.wipe_local and not args.flatten_ibkr:
        parser.error("Nothing to do. Use --wipe-local and/or --flatten-ibkr")

    flattened_ok = None
    restricted_map: Dict[str, str] = {}

    if args.flatten_ibkr:
        print("\n=== IBKR Flatten (Paper) ===")
        if args.confirm != "YES" and not args.dry_run:
            raise SystemExit("Refusing to flatten without --confirm YES (or use --dry-run)")
        if not HAVE_IB:
            raise SystemExit("ib_insync not installed. pip install ib-insync")

        ib = connect_ib(args.host, args.port, args.client_id)
        try:
            if not is_paper_account(ib) and not args.dry_run:
                print("⚠️ This does NOT look like a paper account (managedAccounts != DU*). Aborting flatten.")
                ib.disconnect()
                if args.wipe_local:
                    print("(You can still use --wipe-local to reset local files.)")
                raise SystemExit(1)

            n_cancel = cancel_all_open_orders(ib)
            n_flat, restricted_map, placed_trades = flatten_stock_positions(ib, dry_run=args.dry_run)
            if restricted_map:
                print("⚠️ Compliance-restricted symbols detected:")
                for s, why in restricted_map.items():
                    print(f"   - {s}: {why[:140]}...")
                atomic_write_json(RESTRICTED_PATH, restricted_map)

            flattened_ok = True
            if args.dry_run:
                print("[DRY-RUN] No orders were sent.")
            else:
                print(f"✅ Cancelled {n_cancel} open orders; placed {n_flat} flatten orders.")

            # CHASE mode first (preferred on paper)
            if args.chase and not args.dry_run:
                excl = set(restricted_map.keys())
                print(f"🚓 Chase for up to {args.chase}s (excluding restricted: {sorted(excl)})...")
                flattened_ok = chase_to_zero(ib, args.chase, restricted_syms=excl)
                print("✅ Positions are zero." if flattened_ok else "⏰ Chase timed out.")

            # Legacy wait mode
            if (not flattened_ok) and args.wait_zero and not args.dry_run:
                from time import monotonic
                end = monotonic() + args.wait_zero
                while monotonic() < end:
                    left = []
                    for p in ib.positions():
                        if (p.contract.secType or 'STK').upper() != 'STK':
                            continue
                        sym = p.contract.symbol
                        if sym in restricted_map:
                            continue
                        if int(p.position or 0) != 0:
                            left.append((sym, int(p.position)))
                    if not left:
                        flattened_ok = True
                        break
                    ib.sleep(0.5)
                if not flattened_ok:
                    print(f"⏳ Still open after wait: {left}")
        finally:
            try: ib.disconnect()
            except Exception: pass

    def do_wipe():
        print("\n=== Local Reset ===")
        backup_dir = wipe_local_state()
        if backup_dir:
            print(f"📦 Backed up existing files to ./{backup_dir}")
        print("✅ Local JSON state initialized.")

    if args.wipe_local and not args.then_wipe:
        do_wipe()

    if args.then_wipe and args.flatten_ibkr:
        if flattened_ok or args.wipe_even_if_restricted:
            if (flattened_ok is False) and not args.wipe_even_if_restricted:
                print("❌ Not wiping local because flatten/wait/chase did not complete.")
            else:
                do_wipe()
        else:
            print("❌ Not wiping local (flatten step was skipped or failed). Use --wipe-even-if-restricted to force.")

    print("\nDone.")
