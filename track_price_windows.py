#track_price_windows.py
import json
import os
import pandas as pd
import numpy as np
from collections import defaultdict

MIN_TRADES_PER_BIN = 2
TRADE_PATH = "exports/live_trades.json"
OUTPUT_PATH = "exports/price_window_summary.json"
BEST_THRESHOLD_PCT = 0.03  # 3%
MIN_WORST_LOSS_PCT = -5.0  # PnL % below this is considered a bad entry
MIN_BEST_GAIN_PCT = 3.0     # Consider best entry if avg pnl_pct > +3% (even with < MIN_TRADES)

def load_all_closed_trades(path):
    if not os.path.exists(path):
        print(f"❌ File not found: {path}")
        return []

    with open(path, "r") as f:
        data = json.load(f)

    trades = []
    for day in data.values():
        trades.extend([t for t in day.get("trades", []) if t.get("status") == "closed"])
    return trades

def _safe_idxmin(s: pd.Series):
    """Return idxmin on numeric, NA-coerced series; None if all NA or empty."""
    s = pd.to_numeric(s, errors="coerce")
    if s.notna().any():
        return s.idxmin()
    return None

def _safe_get_float(x):
    """Return float(x) if finite; else None."""
    try:
        v = float(x)
        if np.isfinite(v):
            return v
    except Exception:
        pass
    return None
def analyze_price_windows(trades):
    if not trades:
        print("⚠️ No trades to analyze.")
        return {}

    df = pd.DataFrame(trades)
    if df.empty or not all(col in df.columns for col in ["entry_price", "pnl", "pnl_pct", "symbol"]):
        print("⚠️ Required fields missing in trade data.")
        return {}

    summary = defaultdict(dict)

    # Group by symbol and entry_price to compute stats
    grouped = df.groupby(["symbol", "entry_price"]).agg(
        avg_pnl=("pnl", "mean"),
        avg_pnl_pct=("pnl_pct", "mean"),
        count=("pnl", "count")
    ).reset_index()

    for symbol, group in grouped.groupby("symbol"):
        best_price = None
        worst_price = None
        cz_low, cz_high = None, None

        # === Find worst price
        # NEW
        worst_idx = _safe_idxmin(group["avg_pnl_pct"])
        if worst_idx is None:
            # nothing to do for this group — all NA
            continue
        worst_row = group.loc[worst_idx]
        worst_val = _safe_get_float(worst_row.get("avg_pnl_pct"))
        if worst_val is None:
            # still nothing usable — skip gracefully
            continue

        if worst_row["avg_pnl_pct"] <= MIN_WORST_LOSS_PCT:
            worst_price = round(worst_row["entry_price"], 2)

        # === Try to find strong best price
        group_filtered = group[group["count"] >= MIN_TRADES_PER_BIN]
        if not group_filtered.empty:
            best_row = group_filtered.loc[group_filtered["avg_pnl"].idxmax()]
            if best_row["avg_pnl_pct"] >= MIN_BEST_GAIN_PCT:
                best_price = round(best_row["entry_price"], 2)
        else:
            # Fallback: look for single strong profitable entry
            strong_rows = group[group["avg_pnl_pct"] >= MIN_BEST_GAIN_PCT]
            if not strong_rows.empty:
                best_row = strong_rows.loc[strong_rows["avg_pnl"].idxmax()]
                best_price = round(best_row["entry_price"], 2)

        # === Confidence Zone Calculation
        if best_price:
            cz_low = round(best_price * (1 - BEST_THRESHOLD_PCT), 2)
            cz_high = round(best_price * (1 + BEST_THRESHOLD_PCT), 2)

            # Suppress confidence zone if worst overlaps
            if worst_price and cz_low <= worst_price <= cz_high:
                cz_low, cz_high = None, None

        # Skip if nothing useful found
        if best_price is None and worst_price is None:
            continue

        summary[symbol] = {
            "best_entry": best_price,
            "worst_entry": worst_price,
            "confidence_zone": [cz_low, cz_high] if cz_low and cz_high else [None, None]
        }

    return summary


def save_summary(data, path):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"✅ Summary saved to {path} — {len(data)} symbols processed.")


if __name__ == "__main__":
    all_trades = load_all_closed_trades(TRADE_PATH)
    summary = analyze_price_windows(all_trades)
    save_summary(summary, OUTPUT_PATH)
