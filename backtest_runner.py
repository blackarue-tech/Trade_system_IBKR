import json
import pandas as pd
from datetime import datetime
from modules.ai_signals_debug import get_strategy_signals
from modules.model_utils import add_technical_indicators

INPUT_PATH = "inputs/test_trades_enriched_historical.json"
OUTPUT_PATH = "outputs/strategy_eval_summary.csv"

def load_test_trades(path=INPUT_PATH):
    with open(path) as f:
        return json.load(f)["trades"]

def enrich_and_score(trades):
    results = []
    for trade in trades:
        required = ["RSI", "MA_fast", "MA_slow", "volatility", "high", "entry_price"]
        if not all(k in trade for k in required):
            continue

        row = pd.DataFrame([{
            "RSI": trade["RSI"],
            "MA_fast": trade["MA_fast"],
            "MA_slow": trade["MA_slow"],
            "volatility": trade["volatility"],
            "close": trade["entry_price"],
            "high": trade["high"],
        }], index=[pd.to_datetime(trade["entry_time"])])

        try:
            scored = get_strategy_signals(trade["symbol"], row, min_confidence=0.0, debug=True)
            if not scored.empty:
                s = scored.iloc[0]
                results.append({
                    "symbol": trade["symbol"],
                    "entry_time": trade["entry_time"],
                    "entry_price": trade["entry_price"],
                    "exit_price": trade["exit_price"],
                    "pnl": trade["exit_price"] - trade["entry_price"],
                    "outcome": "Win" if trade["exit_price"] > trade["entry_price"] else "Loss" if trade["exit_price"] < trade["entry_price"] else "Flat",
                    "signal": s["signal"],
                    "confidence": s["confidence"],
                    "strategies": "+".join(s["strategies"])
                })
        except Exception as e:
            print(f"❌ Error scoring {trade['symbol']}: {e}")

    return pd.DataFrame(results)

def main():
    trades = load_test_trades()
    df = enrich_and_score(trades)
    if not df.empty:
        df.to_csv(OUTPUT_PATH, index=False)
        print("✅ Strategy evaluation saved to:", OUTPUT_PATH)
    else:
        print("⚠️ No valid strategy signals matched test trades.")

if __name__ == "__main__":
    main()
