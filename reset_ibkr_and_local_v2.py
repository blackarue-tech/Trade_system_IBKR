"""
reset_ibkr_and_local_v2.py

Enhancements over v1:
- SMART routing by conId to avoid direct-routing rejections
- Detect compliance blocks (e.g., error 201 / Rule 144) and record restricted symbols
- Optional wait for positions to reach zero (excluding restricted) before wiping local
- Option to auto-wipe local after successful flatten (in the right sequence)

Usage examples
--------------
# Flatten paper, wait up to 45s for zero, then wipe local
python reset_ibkr_and_local_v2.py --flatten-ibkr --confirm YES --wait-zero 45 --then-wipe

# Flatten only (preview)
python reset_ibkr_and_local_v2.py --flatten-ibkr --dry-run

# Wipe local only
python reset_ibkr_and_local_v2.py --wipe-local

# Flatten even if some symbols are compliance-restricted, then still wipe local
python reset_ibkr_and_local_v2.py --flatten-ibkr --confirm YES --then-wipe --wipe-even-if-restricted
"""
from __future__ import annotations
import os, json, time, shutil, re
from datetime import datetime
from typing import List, Dict, Any, Tuple, Set

# ---- Optional dependency: ib_insync for IBKR actions ----
try:
    from ib_insync import IB, MarketOrder, Contract
    HAVE_IB = True
except Exception:
    HAVE_IB = False

# ---------- Config ----------
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

# ---------- Helpers ----------
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

# ---------- IBKR helpers ----------
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


def smart_stock_contract_from_conId(conId: int, currency: str | None = None, primaryExchange: str | None = None) -> Contract:
    # Build a SMART-routed stock contract using conId to avoid symbol ambiguity and direct routing
    c = Contract()
    c.secType = 'STK'
    c.conId = int(conId)
    c.exchange = 'SMART'
    c.currency = currency or 'USD'
    if primaryExchange:
        c.primaryExchange = primaryExchange
    return c


def flatten_stock_positions(
    ib: IB,
    *,
    dry_run: bool = False,
    detect_restrictions: bool = True,
) -> Tuple[int, Dict[str, str]]:
    """Place market orders to flatten STOCK positions. Returns (placed_count, restricted_map)."""
    positions = ib.positions()
    stk_positions = [p for p in positions if (p.contract.secType or 'STK').upper() == 'STK' and int(p.position or 0) != 0]
    if not stk_positions:
        print("✅ No stock positions to flatten.")
        return 0, {}

    print(f"🧨 Flattening {len(stk_positions)} stock positions with MARKET orders...")
    placed = 0
    placed_trades = []

    for p in stk_positions:
        qty = int(p.position or 0)
        side = 'SELL' if qty > 0 else 'BUY'
        # SMART by conId
        con = smart_stock_contract_from_conId(p.contract.conId, currency=p.contract.currency or 'USD', primaryExchange=getattr(p.contract, 'primaryExchange', None))
        try:
            ib.qualifyContracts(con)
        except Exception:
            # If qualify fails for some reason, fall back to provided contract
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

    restricted: Dict[str, str] = {}
    if not dry_run and detect_restrictions and placed_trades:
        # Give IB time to post status/logs
        for _ in range(10):
            ib.reqOpenOrders(); ib.reqExecutions(); ib.sleep(0.3)
        # Scan all trades for compliance cancellations (e.g., error 201 / Rule 144)
        pattern_201 = re.compile(r"(?:Error\s+201|Rule\s*144|micro-?cap|compliance)", re.I)
        for tr in ib.trades():
            try:
                status = getattr(tr.orderStatus, 'status', '') or ''
                sym = getattr(tr.contract, 'symbol', '') or ''
                adv = getattr(tr, 'advancedError', '') or ''
                # Also search in log messages
                logtxt = "\n".join([getattr(e, 'message', '') or '' for e in getattr(tr, 'log', [])])
                txt = f"{adv}\n{logtxt}"
                if status.lower() in ("cancelled", "inactive") and pattern_201.search(txt):
                    restricted[sym] = (adv or logtxt or "Compliance/Rule 144 restriction").strip()
            except Exception:
                pass

    return placed, restricted


def wait_positions_zero(ib: IB, timeout_sec: int, *, exclude: Set[str] | None = None) -> bool:
    exclude = exclude or set()
    deadline = time.monotonic() + max(0, int(timeout_sec))
    while True:
        remaining = []
        for p in ib.positions():
            try:
                if (p.contract.secType or 'STK').upper() != 'STK':
                    continue
                sym = p.contract.symbol
                if sym in exclude:
                    continue
                if int(p.position or 0) != 0:
                    remaining.append((sym, int(p.position)))
            except Exception:
                continue
        if not remaining:
            return True
        if time.monotonic() >= deadline:
            print(f"⏳ Still open after wait: {remaining}")
            return False
        ib.sleep(0.5)


# ---------- CLI ----------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reset local state and/or flatten IBKR paper account with SMART routing and compliance handling")
    parser.add_argument("--wipe-local", action="store_true", help="Wipe/initialize local JSON state with backups")
    parser.add_argument("--flatten-ibkr", action="store_true", help="Cancel open orders and liquidate STOCK positions via SMART routing")
    parser.add_argument("--confirm", type=str, default="", help='Type YES to confirm placing market orders for flatten step')
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without sending any orders")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)  # paper default
    parser.add_argument("--client-id", type=int, default=777)
    parser.add_argument("--wait-zero", type=int, default=0, help="Seconds to wait for positions to reach zero (excluding restricted)")
    parser.add_argument("--then-wipe", action="store_true", help="After successful flatten/wait-zero, wipe local files")
    parser.add_argument("--wipe-even-if-restricted", action="store_true", help="Allow local wipe even if some symbols are compliance-restricted")

    args = parser.parse_args()

    if not args.wipe_local and not args.flatten_ibkr:
        parser.error("Nothing to do. Use --wipe-local and/or --flatten-ibkr")

    # --- (A) Flatten IBKR first (safer ordering) ---
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
            pap = is_paper_account(ib)
            if not pap and not args.dry_run:
                print("⚠️ This does NOT look like a paper account (managedAccounts != DU*). Aborting flatten.")
                ib.disconnect()
                if args.wipe_local:
                    print("(You can still use --wipe-local to reset local files.)")
                raise SystemExit(1)

            n_cancel = cancel_all_open_orders(ib)
            n_flat, restricted_map = flatten_stock_positions(ib, dry_run=args.dry_run)
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

            if args.wait_zero and not args.dry_run:
                excl = set(restricted_map.keys())
                print(f"⏳ Waiting up to {args.wait_zero}s for positions to reach zero (excluding restricted: {sorted(excl)})...")
                done = wait_positions_zero(ib, args.wait_zero, exclude=excl)
                print("✅ Positions are zero." if done else "⏰ Timed out waiting for zero positions.")
                flattened_ok = done
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    # --- (B) Wipe local ---
    def do_wipe():
        print("\n=== Local Reset ===")
        backup_dir = wipe_local_state()
        if backup_dir:
            print(f"📦 Backed up existing files to ./{backup_dir}")
        print("✅ Local JSON state initialized.")

    if args.wipe_local and not args.then_wipe:
        # Immediate wipe if user requested and not sequencing after flatten
        do_wipe()

    if args.then_wipe and args.flatten_ibkr:
        # Only wipe after flatten step completes
        if flattened_ok or args.wipe_even_if_restricted:
            if (flattened_ok is False) and not args.wipe_even_if_restricted:
                print("❌ Not wiping local because flatten/wait-zero did not complete and --wipe-even-if-restricted not set.")
            else:
                do_wipe()
        else:
            print("❌ Not wiping local (flatten step was skipped or failed). Use --wipe-even-if-restricted to force.")

    print("\nDone.")
