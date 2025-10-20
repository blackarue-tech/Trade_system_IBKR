# force_flatten_ibkr.py
# Aggressive flatten: cancel per-symbol working orders, submit SMART MKT with outsideRth,
# retry until size == 0 or max attempts hit. Optional direct routing escalation.
#
# Examples:
#   python force_flatten_ibkr.py
#   python force_flatten_ibkr.py --host 127.0.0.1 --port 4002 --client 1023
#   python force_flatten_ibkr.py --outsideRth always --hammer 6
#   python force_flatten_ibkr.py --escalate-direct-after 3   # try direct after 3 failed attempts
#   python force_flatten_ibkr.py --dry-run
#
# NOTE: Uses MARKET orders. For paper trading only.

import argparse
import sys
from time import sleep
from datetime import datetime, time as dtime
try:
    import pytz
except Exception:
    pytz = None  # outsideRth 'auto' still works if Toronto local time is fine

def toronto_now_time():
    if pytz:
        return datetime.now(pytz.timezone("America/Toronto")).time()
    return datetime.now().time()

def is_rth():
    t = toronto_now_time()
    return dtime(9,30) <= t <= dtime(16,0)

def main():
    ap = argparse.ArgumentParser(description="Force flatten all IBKR positions to zero.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client", type=int, default=1023)
    ap.add_argument("--outsideRth", choices=["auto","always","never"], default="auto",
        help="Permit fills outside regular hours (auto uses True when not RTH).")
    ap.add_argument("--hammer", type=int, default=4, help="Attempts per symbol before giving up (default 4)")
    ap.add_argument("--sleep-per-attempt", type=float, default=3.0, help="Seconds to wait after each attempt")
    ap.add_argument("--escalate-direct-after", type=int, default=0,
        help="After N failed attempts, try direct routing to primaryExchange (0 = never).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        from ib_insync import IB, Stock, MarketOrder
    except Exception as e:
        print("ERROR: ib_insync is required. pip install ib-insync")
        print(f"Detail: {e}")
        sys.exit(1)

    # Decide outsideRth flag
    def outside_rth_flag():
        if args.outsideRth == "always":
            return True
        if args.outsideRth == "never":
            return False
        return not is_rth()  # auto

    def cancel_symbol_orders(ib: "IB", symbol: str):
        count = 0
        for tr in ib.trades():
            try:
                c = tr.contract
                if getattr(c, "symbol", None) == symbol:
                    st = (tr.orderStatus.status or "").lower()
                    if st in ("presubmitted","submitted","pendingsubmit","pendingcancel","inactive"):
                        ib.cancelOrder(tr.order)
                        count += 1
            except Exception:
                pass
        if count:
            ib.sleep(0.25)
        return count

    def place_flat(ib: "IB", p, route: str):
        """Place a MARKET order to flatten one position object p."""
        qty = int(getattr(p, "position", 0) or 0)
        if qty == 0:
            return None
        action = "SELL" if qty > 0 else "BUY"

        c0 = p.contract
        use_contract = c0
        # Prefer SMART for stocks; direct if requested
        if c0.secType == "STK":
            exch = "SMART" if route == "smart" else (getattr(c0, "primaryExchange", None) or getattr(c0, "exchange", None) or "SMART")
            cur = getattr(c0, "currency", "USD") or "USD"
            use_contract = Stock(c0.symbol, exch, cur)

        # qualify and send
        try:
            ib.qualifyContracts(use_contract)
        except Exception:
            # Some positions already have fully qualified contracts; proceed.
            pass

        o = MarketOrder(action, abs(qty))
        o.tif = "DAY"
        try:
            o.outsideRth = bool(outside_rth_flag())
        except Exception:
            pass

        if args.dry_run:
            print(f"DRY: {action} {abs(qty)} {c0.symbol} via {getattr(use_contract,'exchange','?')} MKT outsideRth={getattr(o,'outsideRth',None)}")
            return None

        tr = ib.placeOrder(use_contract, o)
        print(f"Placed {action} {abs(qty)} MKT for {c0.symbol} via {getattr(use_contract,'exchange','?')} "
              f"(orderId={getattr(tr.order,'orderId',None)}, outsideRth={getattr(o,'outsideRth',None)})")
        return tr

    ib = IB()
    try:
        ib.connect(args.host, args.port, clientId=args.client)
        print(f"Connected {args.host}:{args.port} clientId={args.client} (RTH={is_rth()})")

        positions = ib.positions()
        targets = [p for p in positions if int(getattr(p, "position", 0) or 0) != 0]
        if not targets:
            print("No positions to flatten (already flat).")
            return

        # Work symbol by symbol to avoid crossing orders
        for p in targets:
            symbol = getattr(p.contract, "symbol", "?")
            print(f"\n=== Flatten {symbol} (start qty={int(getattr(p,'position',0))}) ===")
            attempts = 0
            route = "smart"

            while attempts < max(1, args.hammer):
                attempts += 1

                # 1) Cancel working orders on this symbol to avoid interference
                canceled = cancel_symbol_orders(ib, symbol)
                if canceled:
                    print(f"Cancelled {canceled} working order(s) for {symbol}")

                # 2) Re-check remaining position
                ib.sleep(0.2)
                rem = [pp for pp in ib.positions() if getattr(pp.contract, "symbol", None) == symbol]
                if not rem or int(getattr(rem[0], "position", 0) or 0) == 0:
                    print(f"{symbol}: already flat.")
                    break

                # 3) Choose routing (optionally escalate to direct)
                if args.escalate_direct_after and attempts > args.escalate_direct_after:
                    route = "direct"

                # 4) Place flatten order
                place_flat(ib, rem[0], route=route)

                # 5) Wait and verify
                ib.sleep(max(0.2, args.sleep_per_attempt))
                rem = [pp for pp in ib.positions() if getattr(pp.contract, "symbol", None) == symbol]
                qty_now = int(getattr(rem[0], "position", 0) or 0) if rem else 0
                print(f"{symbol}: qty after attempt {attempts} = {qty_now}")

                if qty_now == 0:
                    print(f"✅ {symbol} flat.")
                    break

            else:
                print(f"⚠️ {symbol} still not flat after {attempts} attempt(s). "
                      f"{'Direct routing attempted; check TWS settings if auto-cancel.' if route=='direct' else 'Consider --escalate-direct-after or manual review in TWS.'}")

        # Final account check
        ib.sleep(0.5)
        remaining = [pp for pp in ib.positions() if int(getattr(pp, "position", 0) or 0) != 0]
        if remaining:
            print("\n⚠️ Still non-zero positions:")
            for pp in remaining:
                print(f" - {pp.contract.symbol}: {int(pp.position)} ({pp.contract.secType}/{pp.contract.exchange})")
            print("If symbols are HALTED or outsideRth is not allowed, they may not fill until RTH opens.")
        else:
            print("\n✅ Account is FLAT.")

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    main()
