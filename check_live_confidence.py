import pandas as pd
from modules.model_utils import add_technical_indicators
from modules.price_data import get_price_data
from modules.ai_signals_pro import get_strategy_signals
from modules.questrade import get_symbol_id

SYMBOLS = ["MU", "AAPL", "NVDA", "TSLA"]
MIN_CONFIDENCE = 0.0  # show everything for inspection

def check_confidence(symbol):
    try:
        symbol_id = get_symbol_id(symbol)
        df = get_price_data(symbol_id, days=1, interval="OneMinute")
        df = add_technical_indicators(df)

        signals_df = get_strategy_signals(symbol, df, min_confidence=MIN_CONFIDENCE)
        if signals_df is None or signals_df.empty:
            print(f"❌ No signals for {symbol}")
            return

        latest = signals_df.iloc[-1]
        print(f"🔎 {symbol} — Confidence: {latest['confidence']:.2f} | Strategies: {latest['strategies']}")
    except Exception as e:
        print(f"⚠️ Error for {symbol}: {e}")

if __name__ == "__main__":
    print("📊 Checking confidence levels for current symbols...")
    for symbol in SYMBOLS:
        check_confidence(symbol)
