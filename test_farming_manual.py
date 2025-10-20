# test_farming_manual.py

import pandas as pd
from modules.price_data import get_price_data

def is_farming_momentum(symbol, df, window=15, slope_threshold=0.5, green_pct=0.7):
    """
    Detects a steady upward move over 'window' candles.
    """
    if df is None or len(df) < window:
        print(f"{symbol} skipped — insufficient data: {len(df)} rows.")
        return False

    try:
        recent = df.iloc[-window:]
        closes = recent["close"].values
        slope = (closes[-1] - closes[0]) / closes[0] * 100
        green_candles = sum(recent["close"] > recent["open"])
        green_ratio = green_candles / window

        if slope >= slope_threshold and green_ratio >= green_pct:
            print(f"✅ {symbol} farming momentum: slope={slope:.2f}%, green_ratio={green_ratio:.2f}")
        else:
            print(f"❌ {symbol} not farming: slope={slope:.2f}%, green_ratio={green_ratio:.2f}")
        return slope >= slope_threshold and green_ratio >= green_pct
    except Exception as e:
        print(f"⚠️ Error in momentum calc: {e}")
        return False

# Manual test loop
symbol = "HIMZ"
df = get_price_data(symbol, days=1, interval="minute")

if df is None or df.empty:
    print("❌ Failed to load data.")
else:
    df = df.reset_index(drop=True)  # ensure no weird index
    print(f"📊 Loaded {len(df)} rows for {symbol}")
    for i in range(15, len(df)):
        window_df = df.iloc[i - 15:i].copy()
        ts = df.iloc[i]["datetime"]
        print(f"\n🕒 {ts} - checking momentum window ending at this candle:")
        is_farming_momentum(symbol, window_df)

