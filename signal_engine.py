# signal_engine.py – Dynamic Top 100 Signal Daemon

from utils import atomic_save_json, update_system_status
import os, json, time
from datetime import datetime
import pytz
from pytz import timezone
import pandas as pd
from modules.price_data import get_price_data
from modules.ai_signals_pro import get_strategy_signals
from modules.questrade import get_symbol_id

SIGNAL_CONFIG_PATH = "inputs/ai_strategy_configs/signal_config.json"
SYMBOL_CSV_PATH = "inputs/top_100_us_stocks.csv"
SIGNAL_OUTPUT_PATH = "exports/live_signals.json"
LOCAL_TZ = timezone("America/Toronto")

symbol_id_cache = {}

def load_config(path):
    with open(path, "r") as f:
        return json.load(f)

def load_symbols():
    df = pd.read_csv(SYMBOL_CSV_PATH)
    return df["symbol"].dropna().unique().tolist()

def save_signals(data):
    atomic_save_json(data, SIGNAL_OUTPUT_PATH)

def signal_engine_loop():
    print("📡 Starting Signal Engine Loop...")
    try:
        while True:
            config = load_config(SIGNAL_CONFIG_PATH)
            min_conf = config.get("min_confidence", 0.5)
            top_n = config.get("top_n_signals", 4)
            refresh_interval = config.get("refresh_interval_sec", 300)  # default 5 min
            thresholds = config.get("thresholds", {})
            filters = config.get("filters", {})

            symbols = load_symbols()
            all_signals = []

            for symbol in symbols:
                print(f"🔍 Scanning {symbol}...")

                # Resolve symbolId dynamically
                if symbol in symbol_id_cache:
                    symbol_id = symbol_id_cache[symbol]
                else:
                    try:
                        symbol_id = get_symbol_id(symbol)
                        symbol_id_cache[symbol] = symbol_id
                        print(f"🔁 Resolved symbolId for {symbol}: {symbol_id}")
                    except Exception as e:
                        print(f"❌ Failed to resolve symbolId for {symbol}: {e}")
                        continue

                # Retry fetching candles
                max_retries, retry_delay = 3, 3
                for attempt in range(max_retries):
                    df = get_price_data(symbol_id, days=1, interval="OneMinute")
                    if df is not None and not df.empty:
                        break
                    print(f"⚠️ Attempt {attempt+1} failed for {symbol}, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)

                if df is None or df.empty:
                    print(f"❌ No data for {symbol} after {max_retries} attempts.")
                    continue

                signals_df = get_strategy_signals(symbol, df, min_confidence=0.0, config=thresholds)
                if signals_df.empty:
                    continue

                top_row = signals_df.sort_values("confidence", ascending=False).iloc[0]
                confidence = top_row["confidence"]
                if confidence < min_conf:
                    print(f"⚠️ {symbol} max confidence {confidence:.2f} < threshold {min_conf}")
                    continue

                # Timezone-safe timestamp
                timestamp = top_row["datetime"]
                if not pd.Timestamp(timestamp).tzinfo:
                    timestamp = pd.Timestamp(timestamp).tz_localize("UTC").tz_convert(LOCAL_TZ)
                else:
                    timestamp = pd.Timestamp(timestamp).tz_convert(LOCAL_TZ)

                # Contextual filters
                if "volume" in top_row and top_row["volume"] < filters.get("min_volume", 0):
                    print(f"❌ {symbol} skipped: volume {top_row['volume']:.0f} < {filters['min_volume']}")
                    continue

                if "close" in top_row and top_row["close"] < filters.get("min_price", 0):
                    print(f"❌ {symbol} skipped: price {top_row['close']:.2f} < {filters['min_price']}")
                    continue

                if "rvol" in top_row and top_row["rvol"] < filters.get("min_rvol", 0):
                    print(f"❌ {symbol} skipped: RVOL {top_row['rvol']:.2f} < {filters['min_rvol']}")
                    continue

                print(f"🧪 {symbol} best confidence: {confidence:.2f} strategies: {top_row['strategies']} @ {timestamp}")
                all_signals.append({
                    "symbol": symbol,
                    "symbolId": symbol_id,
                    "confidence": round(confidence, 3),
                    "strategies": top_row["strategies"],
                    "timestamp": timestamp.isoformat()
                })

            all_signals.sort(key=lambda x: x["confidence"], reverse=True)
            top_signals = all_signals[:top_n]
            print(f"✅ Top Signals: {[s['symbol'] for s in top_signals]}")
            save_signals(top_signals)
            print(f"⏱️ Sleeping for {refresh_interval}s...\n")
            update_system_status("signal_engine", {
                "top_symbols": [s["symbol"] for s in top_signals],
                "count": len(top_signals)
            })
            time.sleep(refresh_interval)
    except KeyboardInterrupt:
        print("🛑 Stopped live_executor loop.")
    except Exception as e:
        print(f"❌ Unexpected error in loop: {e}")
        time.sleep(5)
if __name__ == "__main__":
    signal_engine_loop()