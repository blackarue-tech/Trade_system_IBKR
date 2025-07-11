import json
import pandas as pd
from datetime import datetime
from modules.model_utils import add_technical_indicators
from modules.price_data import get_price_data

INPUT_PATH = "inputs/test_trades_enriched_historical.json"
OUTPUT_PATH = "inputs/trade_merge_enriched.json"
TOKEN_PATH = "C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json"

def load_trades():
    with open(INPUT_PATH) as f:
        return json.load(f)["trades"]

def enrich_trade(trade):
    symbol_id = trade.get("symbolId")
    symbol = trade.get("symbol")
    entry_time = pd.to_datetime(trade.get("entry_time"), utc=True)

    if not symbol_id or entry_time is pd.NaT:
        print(f"⚠️ Skipping {symbol} due to missing data.")
        return None

    df = get_price_data(symbol_id, days=30, token_path=TOKEN_PATH)
    if df is None or df.empty:
        print(f"❌ Failed to fetch price data for {symbol}")
        return None

    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "datetime"
    df.sort_index(inplace=True)

    df = add_technical_indicators(df)

    valid_rows = df[df.index <= entry_time].dropna(subset=["MA20", "volatility", "rsi", "bb_width"])
    if valid_rows.empty:
        print(f"⚠️ No valid indicator row before {entry_time} for {symbol}")
        return None

    row = valid_rows.iloc[-1]
    enriched_time = row.name

    trade.update({
        "datetime": enriched_time.isoformat(),
        "MA20": row.get("MA20"),
        "volatility": row.get("volatility"),
        "rsi": row.get("rsi"),
        "bb_width": row.get("bb_width")
    })

    print(f"✅ Enriched {symbol} at nearest date {enriched_time}")
    return trade

def main():
    trades = load_trades()
    enriched = []
    for trade in trades:
        try:
            enriched_trade = enrich_trade(trade)
            if enriched_trade:
                enriched.append(enriched_trade)
        except Exception as e:
            print(f"❌ Error enriching {trade.get('symbol')}: {e}")

    with open(OUTPUT_PATH, "w") as f:
        json.dump({"trades": enriched}, f, indent=2)
        print(f"✅ Enriched trades saved to {OUTPUT_PATH}")

if __name__ == "__main__":
    main()
