# reset_ibkr_day.py  (SMART routing)
# Cancel ALL working orders and FLATTEN ALL positions to zero.
# Examples:
#   python reset_ibkr_day.py                       # SMART routing (default)
#   python reset_ibkr_day.py --route direct        # allow direct routing if needed
#   python reset_ibkr_day.py --dry-run

import argparse, sys
from time import sleep

def main():
    ap = argparse.ArgumentParser(description="Reset IBKR test day: cancel orders + flatten positions.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client", type=int, default=1023)
    ap.add_argument("--route", choices=["smart","direct"], default="smart",
                    help="Order routing for flattening. 'smart' avoids 10311 warnings (default).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait-secs", type=int, default=10)
    ap.add_argument("--max-wait", type=int, default=60)
    args = ap.parse_args()

    try:
        from ib_insync import IB, MarketOrder, Stock
    except Exception as e:
        print("ERROR: ib_insync is required. pip install ib-insync")
        print(f"Detail: {e}"); sys.exit(1)

    ib = IB()
    try:
        ib.connect(args.host, args.port, clientId=args.client)
        print(f"Connected {args.host}:{args.port} clientId={args.client}")

        # 1) Cancel all working orders
        trades = ib.trades()
        canc = []
        for tr in trades:
            st = (getattr(tr.orderStatus, "status", "") or "").lower()
            if st in ("presubmitted","submitted","pendingsubmit","pendingcancel","inactive"):
                canc.append(tr)
        if canc:
            print(f"Cancelling {len(canc)} working orders...")
            if not args.dry_run:
                for tr in canc:
                    ib.cancelOrder(tr.order)
                ib.sleep(0.3)
            else:
                for tr in canc:
                    print(f" - would cancel orderId={getattr(tr.order,'orderId',None)}")
        else:
            print("No working orders to cancel.")

        # 2) Flatten all positions (route = SMART by default)
        pos = ib.positions()
        to_flatten = [p for p in pos if int(getattr(p,"position",0) or 0) != 0]
        if not to_flatten:
            print("No positions to flatten (already flat).")
        else:
            print(f"Flattening {len(to_flatten)} positions using route={args.route.upper()}...")
            for p in to_flatten:
                qty = int(getattr(p, "position", 0) or 0)
                action = "SELL" if qty > 0 else "BUY"
                contract = p.contract

                # Prefer SMART routing for stocks
                use_contract = contract
                if contract.secType == "STK" and args.route == "smart":
                    try:
                        c = Stock(contract.symbol, "SMART", getattr(contract, "currency", "USD") or "USD")
                        ib.qualifyContracts(c)
                        use_contract = c
                    except Exception as e:
                        # If SMART qualification fails, skip to avoid direct route 10311 auto-cancel
                        print(f" ! Skip {contract.symbol}: SMART qualify failed ({e}); use --route direct to force.")
                        continue

                if args.dry_run:
                    print(f" - would {action} {abs(qty)} {contract.symbol} ({use_contract.exchange}) MKT")
                    continue

                # Qualify if needed (non-STK or direct route)
                try:
                    ib.qualifyContracts(use_contract)
                except Exception:
                    pass

                o = MarketOrder(action, abs(qty))
                # ensure DAY TIF; transmit by default
                o.tif = "DAY"
                tr = ib.placeOrder(use_contract, o)
                print(f"Placed {action} {abs(qty)} MKT for {contract.symbol} via {use_contract.exchange} "
                      f"(orderId={getattr(tr.order,'orderId',None)})")

            if not args.dry_run:
                ib.sleep(max(0.3, args.wait_secs))

        # 3) Confirm flat (best effort)
        if not args.dry_run:
            waited = 0
            while waited < args.max_wait:
                remaining = [pp for pp in ib.positions() if int(getattr(pp, "position", 0) or 0) != 0]
                if not remaining:
                    print("✅ Account is flat.")
                    break
                sleep(1); waited += 1
            else:
                print("⚠️ Still non-zero positions after wait; check TWS Activity Log.")

        print("Done.")
    except Exception as e:
        print(f"ERROR: {e}"); sys.exit(1)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    main()
