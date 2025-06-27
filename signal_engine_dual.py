# signal_engine_dual.py – Dual-threaded Signal Engine (Selection + Monitoring)

import os, json, time, threading
from datetime import datetime
import pandas as pd
import pytz
from pytz import timezone

from modules.price_data import get_price_data
from modules.ai_signals_pro import get_strategy_signals
from modules.questrade import get_symbol_id
from utils import atomic_save_json, update_system_status

SIGNAL_CONFIG_PATH = "inputs/ai_strategy_configs/signal_config.json"
SYMBOL_CSV_PATH = "inputs/top_100_us_stocks.csv"
LIVE_SIGNALS_PATH = "exports/live_signals.json"
FREQ_SIGNALS_PATH = "exports/frequent_signals.json"
LOCAL_TZ = timezone("America/Toronto")
PENALTY_TRACKER_PATH = "exports/penalty_tracker.json"
LIVE_TRADES_PATH = "exports/live_trades.json"

symbol_id_cache = {}

def load_config():
    with open(SIGNAL_CONFIG_PATH, "r") as f:
        return json.load(f)

def load_symbols():
    df = pd.read_csv(SYMBOL_CSV_PATH)
    return df["symbol"].dropna().unique().tolist()

def load_live_trades():
    if os.path.exists(LIVE_TRADES_PATH):
        with open(LIVE_TRADES_PATH, "r") as f:
            return json.load(f)
    return {}

def get_symbol_id_cached(symbol):
    if symbol not in symbol_id_cache:
        symbol_id_cache[symbol] = get_symbol_id(symbol)
    return symbol_id_cache[symbol]

def save_signals(data, path):
    atomic_save_json(data, path)

def load_penalty_tracker(path=PENALTY_TRACKER_PATH):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def select_top_symbols_loop():
    while True:
        print("🧠 Running Top Symbol Selection (10-min loop)")
        config = load_config()
        min_conf = config.get("min_confidence", 0.4)
        top_n = config.get("top_n_signals", 4)
        refresh = config.get("refresh_interval_sec", 600)
        thresholds = config.get("thresholds", {})
        filters = config.get("filters", {})
        start_time = time.time()
        symbols = load_symbols()
        all_signals = []

        for symbol in symbols:
            try:
                print(f"🔍 Scanning {symbol}...")
                symbol_id = get_symbol_id_cached(symbol)
                df = get_price_data(symbol_id, days=1, interval="OneMinute")
                print(f"📊 {symbol} data: {df.shape} rows")
                if df is None or df.empty:
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
                penalty_tracker = load_penalty_tracker()
                penalty_info = penalty_tracker.get(symbol)
                
                if penalty_info and penalty_info.get("recovery_status") != "approved":
                    print(f"🚫 {symbol} is currently penalized. Skipping.")
                    continue
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

                

                all_signals.append({
                    "symbol": symbol,
                    "symbolId": symbol_id,
                    "confidence": round(confidence, 3),
                    "strategies": top_row["strategies"],
                    "timestamp": timestamp.isoformat()
                })

            except Exception as e:
                print(f"⚠️ {symbol} failed: {e}")

        all_signals.sort(key=lambda x: x["confidence"], reverse=True)
        top_signals = all_signals[:top_n]
        save_signals(top_signals, LIVE_SIGNALS_PATH)
        # Define recent_trades as an empty list or fetch actual recent trades if available       
        
        print(f"✅ Top Signals: {[s['symbol'] for s in top_signals]}")
        update_system_status("signal_engine_top", {"symbols": [s["symbol"] for s in top_signals]})
        elapsed = time.time() - start_time
        sleep_time = max(0, refresh - elapsed)
        print(f"⏳ Sleeping for {sleep_time:.2f}s until next top selection round")
        time.sleep(sleep_time)

def monitor_frequent_signals_loop():
    while True:
        try:
            print("📡 Monitoring Live Signals (120s loop)")
            if not os.path.exists(LIVE_SIGNALS_PATH):
                print("⚠️ No live_signals.json yet.")
                time.sleep(120)
                continue

            config = load_config()
            min_conf = config.get("min_confidence", 0.4)
            thresholds = config.get("thresholds", {})

            with open(LIVE_SIGNALS_PATH, "r") as f:
                symbols_data = json.load(f)

            active_signals = []

            for entry in symbols_data:
                symbol = entry["symbol"]
                symbolId = entry["symbolId"]
                try:
                    df = get_price_data(symbolId, days=1, interval="OneMinute")
                    if df is None or df.empty:
                        continue
                    df = df.copy()
                    signals_df = get_strategy_signals(symbol, df, min_confidence=min_conf, config=thresholds)
                    if signals_df.empty:
                        continue

                    live = signals_df[signals_df["confidence"] >= min_conf]
                    for _, row in live.iterrows():
                        ts = pd.Timestamp(row["datetime"])
                        if not ts.tzinfo:
                            ts = ts.tz_localize("UTC").tz_convert(LOCAL_TZ)
                        active_signals.append({
                            "symbol": symbol,
                            "symbolId": symbolId,
                            "confidence": round(row["confidence"], 3),
                            "strategies": row["strategies"],
                            "timestamp": ts.isoformat()
                        })
                except Exception as e:
                    print(f"❌ {symbol} monitor error: {e}")

            active_signals.sort(key=lambda x: x["confidence"], reverse=True)
            save_signals(active_signals, FREQ_SIGNALS_PATH)
            update_system_status("signal_engine_monitor", {"count": len(active_signals)})
            print(f"📈 {len(active_signals)} active signals logged.")
        except Exception as e:
            print(f"⚠️ Monitor error: {e}")
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=select_top_symbols_loop, daemon=True).start()
    threading.Thread(target=monitor_frequent_signals_loop, daemon=True).start()
    while True:
        time.sleep(60)
