"""
reset_ibkr_and_local.py

One-stop utility to:
 1) Wipe/initialize local state files (safe backup first)
 2) Flatten IBKR (Paper) account: cancel all open orders + liquidate all STOCK positions

⚠️ WARNING: The flatten step PLACES MARKET ORDERS. Use --dry-run to preview.

Usage examples:
  # Local wipe only
  python reset_ibkr_and_local.py --wipe-local

  # Paper flatten only (stocks) + confirm
  python reset_ibkr_and_local.py --flatten-ibkr --confirm YES --client-id 777

  # Both: wipe local AND flatten paper
  python reset_ibkr_and_local.py --wipe-local --flatten-ibkr --confirm YES

By default this targets TWS/Gateway paper port 4002 at 127.0.0.1. Override with --host/--port.
"""
from __future__ import annotations
import os, json, time, shutil
from datetime import datetime
from dataclasses import dataclass
from typing import List, Dict, Any

# ---- Optional dependency: ib_insync for IBKR actions ----
try:
    from ib_insync import IB, MarketOrder, Contract
    HAVE_IB = True
except Exception:
    HAVE_IB = False

# ---------- Config ----------
LOCAL_FILES: Dict[str, Any] = {
    # path                         default value after wipe
    "exports/ibkr_orders_log.json": [],
    "exports/ibkr_orders_status.json": [],
    "exports/ibkr_positions.json": {},
    "exports/sync_audit.json": {},
    "exports/exit_failures.json": [],
    "exports/recovery_tracker.json": {},
    "exports/last_exit_tracker.json": {},    
    "exports/polygon_quotes.json": {},
}

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


# ---------- Local wipe ----------
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


# ---------- IBKR flatten (paper) ----------
def connect_ib(host: str, port: int, client_id: int) -> IB:
    if not HAVE_IB:
        raise RuntimeError("ib_insync not installed. pip install ib-insync")
    ib = IB()
    ib.connect(host, port, clientId=int(client_id), timeout=6)
    return ib


def is_paper_account(ib: IB) -> bool:
    try:
        accts = ib.managedAccounts() or []
        # Paper typically uses DU******* accounts
        return all(str(a).upper().startswith("DU") for a in accts) and len(accts) > 0
    except Exception:
        return False


def cancel_all_open_orders(ib: IB):
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


def flatten_stock_positions(ib: IB, *, dry_run: bool = False) -> int:
    ib_positions = ib.positions()
    stk_positions = [p for p in ib_positions if (p.contract.secType or "STK").upper() == "STK" and int(p.position or 0) != 0]
    if not stk_positions:
        print("✅ No stock positions to flatten.")
        return 0
    print(f"🧨 Flattening {len(stk_positions)} stock positions with MARKET orders...")
    placed = 0
    for p in stk_positions:
        qty = int(p.position or 0)
        side = "SELL" if qty > 0 else "BUY"
        # with SMART-routed contract using conId
        con = Contract(conId=p.contract.conId, secType='STK', exchange='SMART', currency=p.contract.currency or 'USD')
        ib.qualifyContracts(con)
        print(f"  {side} {abs(qty)} {con.symbol} @ MKT")
        if dry_run:
            continue
        try:
            o = MarketOrder(side, abs(qty), tif="DAY", outsideRth=True)
            tr = ib.placeOrder(con, o)
            placed += 1
            print(f"    → placed orderId={getattr(tr.order, 'orderId', '?')}")
        except Exception as e:
            print(f"    ⚠️ place failed: {e}")
    if not dry_run:
        # Nudge updates in
        for _ in range(10):
            ib.reqExecutions(); ib.reqPositions(); ib.sleep(0.3)
    return placed


# ---------- CLI ----------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reset local state and/or flatten IBKR paper account")
    parser.add_argument("--wipe-local", action="store_true", help="Wipe/initialize local JSON state with backups")
    parser.add_argument("--flatten-ibkr", action="store_true", help="Cancel open orders and liquidate STOCK positions")
    parser.add_argument("--confirm", type=str, default="", help='Type YES to confirm placing market orders for flatten step')
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without sending any orders")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)  # paper default
    parser.add_argument("--client-id", type=int, default=777)

    args = parser.parse_args()

    if not args.wipe_local and not args.flatten_ibkr:
        parser.error("Nothing to do. Use --wipe-local and/or --flatten-ibkr")

    # --- 1) Local wipe ---
    if args.wipe_local:
        print("\n=== Local Reset ===")
        backup_dir = wipe_local_state()
        if backup_dir:
            print(f"📦 Backed up existing files to ./{backup_dir}")
        print("✅ Local JSON state initialized.")

    # --- 2) IBKR flatten ---
    if args.flatten_ibkr:
        print("\n=== IBKR Flatten (Paper) ===")
        if args.confirm != "YES" and not args.dry_run:
            raise SystemExit("Refusing to flatten without --confirm YES (or use --dry-run)")
        if not HAVE_IB:
            raise SystemExit("ib_insync not installed. pip install ib-insync")

        ib = connect_ib(args.host, args.port, args.client_id)
        try:
            pap = is_paper_account(ib)
            if not pap:
                print("⚠️ This does NOT look like a paper account (managedAccounts != DU*). Aborting.")
                print("    If you really intend to flatten a live account, re-run with --confirm YES --host/--port set correctly.")
                if not args.dry_run:
                    ib.disconnect()
                    raise SystemExit(1)

            n_cancel = cancel_all_open_orders(ib)
            n_flat = flatten_stock_positions(ib, dry_run=args.dry_run)
            if args.dry_run:
                print("[DRY-RUN] No orders were sent.")
            else:
                print(f"✅ Cancelled {n_cancel} open orders; placed {n_flat} flatten orders.")
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    print("\nDone.")
