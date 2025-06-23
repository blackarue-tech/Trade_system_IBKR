#backtest_runner_strategy_winrate.py

import json
import pandas as pd

INPUT_FILE = "inputs/test_trades_enriched_historical.json"
OUTPUT_FILE = "outputs/strategy_winrate_summary.csv"

def load_trades(path=INPUT_FILE):
    with open(path, "r") as f:
        return json.load(f)["trades"]

def compute_winrate_summary(trades):
    df = pd.DataFrame(trades)
    
    # Ensure necessary fields exist
    required_cols = ["strategy", "pnl"]
    if not all(col in df.columns for col in required_cols):
        raise ValueError(f"Missing columns: {set(required_cols) - set(df.columns)}")

    # Create 'win' column: 1 if pnl > 0 else 0
    df["is_win"] = df["pnl"] > 0

    # Group by strategy and compute metrics
    summary = df.groupby("strategy").agg(
        trades=("pnl", "count"),
        wins=("is_win", "sum"),
        win_rate=("is_win", "mean"),
        avg_pnl=("pnl", "mean")
    ).sort_values(by="win_rate", ascending=False)

    # Format win_rate as percentage
    summary["win_rate"] = (summary["win_rate"] * 100).round(2)

    return summary.reset_index()

def main():
    trades = load_trades()
    summary_df = compute_winrate_summary(trades)

    print("\n📊 Strategy Win Rate Summary:")
    print(summary_df)

    # Save results
    summary_df.to_csv(OUTPUT_FILE, index=False)
    print(f"\n✅ Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
