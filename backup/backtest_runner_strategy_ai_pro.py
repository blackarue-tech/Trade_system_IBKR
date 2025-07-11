
# backtest_runner_strategy_ai_pro.py

import json
import pandas as pd
from modules.ai_signals_pro import get_strategy_signals
from modules.price_data import get_price_data
from modules.model_utils import add_technical_indicators
from modules.questrade import get_symbol_id

INPUT_FILE = "inputs/test_trades_enriched_historical_normalized.json"
OUTPUT_FILE = "outputs/backtest_ai_pro_summary.csv"

with open(INPUT_FILE) as f:
    trades = json.load(f)

summary = []

for trade in trades:
    symbol = trade["symbol"]
    entry_time = pd.to_datetime(trade["entry_time"])
    symbol_id = trade.get("symbolId") or get_symbol_id(symbol)

    try:
        df = get_price_data(symbol_id, days=5, interval="OneMinute")
        if df is None or df.empty:
            continue

        df = add_technical_indicators(df)
        signals_df = get_strategy_signals(symbol, df)

        # Find signal near entry
        match = signals_df[signals_df.index == entry_time]
        if not match.empty:
            row = match.iloc[0]
            summary.append({
                "symbol": symbol,
                "entry_time": entry_time,
                "confidence": row["confidence"],
                "signal": row["signal"],
                "strategies_used": ",".join(row.get("strategies_used", []))
            })

    except Exception as e:
        print(f"❌ Error scoring {symbol}: {e}")

if summary:
    df_summary = pd.DataFrame(summary)
    df_summary.to_csv(OUTPUT_FILE, index=False)
    print(f"✅ Saved {len(df_summary)} signals to {OUTPUT_FILE}")
else:
    print("🚫 No valid signals generated from enriched trades.")
