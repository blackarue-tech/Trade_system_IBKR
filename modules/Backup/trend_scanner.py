import pandas as pd
from modules.questrade import get_symbol_id
from modules.price_data import get_price_data

def get_positive_trending_stocks(symbol_list, min_pct_gain=0.2):
    """
    Fetches US stocks from symbol_list trending positive over the last 30 minutes.
    Returns a sorted list of {symbol, symbolId, pct_change}.
    """
    trending_stocks = []

    for symbol in symbol_list:
        try:
            symbol_id = get_symbol_id(symbol)
            df = get_price_data(symbol_id, days=1, interval="OneMinute")
            if df is None or df.empty or "close" not in df.columns:
                continue

            # Filter last 30 minutes
            df_last_30 = df[df["datetime"] >= (pd.Timestamp.utcnow() - pd.Timedelta(minutes=30))]
            if df_last_30.empty:
                continue

            start_price = df_last_30["close"].iloc[0]
            end_price = df_last_30["close"].iloc[-1]

            pct_change = ((end_price - start_price) / start_price) * 100

            if pct_change >= min_pct_gain:
                trending_stocks.append({
                    "symbol": symbol,
                    "symbolId": symbol_id,
                    "pct_change": round(pct_change, 2)
                })
                print(f"✅ {symbol} trending +{pct_change:.2f}% in last 30 min.")

        except Exception as e:
            print(f"⚠️ Error fetching {symbol}: {e}")
            continue

    # Sort descending by percentage gain
    trending_stocks.sort(key=lambda x: x["pct_change"], reverse=True)

    return trending_stocks
