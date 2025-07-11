import pandas as pd
import json
import os
from modules.ai_signals_pro import get_strategy_signals
from modules.price_data import get_price_data
from modules.model_utils import add_technical_indicators
from modules.questrade import get_symbol_id
from datetime import datetime, timedelta

SYMBOLS = ["MU", "AAPL", "NVDA", "TSLA"]
CONFIDENCE_THRESHOLD = 0.0
OUTPUT_PATH = "outputs/simulation_ai_pro_results.csv"
TRADES_JSON = "exports/live_trades.json"
WALLET_PATH = "exports/wallet.json"

EXIT_HOLD_MINUTES = 30
TARGET_GAIN = 0.01   # 1%
STOP_LOSS = -0.006   # -0.6%

today = datetime.now().strftime("%Y-%m-%d")

# Load wallet
def load_wallet():
    if os.path.exists(WALLET_PATH):
        with open(WALLET_PATH) as f:
            return json.load(f)
    return {"capital": 5000.0, "last_updated": None}

def save_wallet(wallet):
    wallet["last_updated"] = datetime.now().isoformat()
    with open(WALLET_PATH, "w") as f:
        json.dump(wallet, f, indent=2)

wallet = load_wallet()

# Load and normalize trade data
trade_data = {}

if os.path.exists(TRADES_JSON):
    with open(TRADES_JSON, "r") as f:
        try:
            raw_data = json.load(f)

            # ✅ Case 1: Old format — flat list of trades
            if isinstance(raw_data, list):
                print("⚠️ Detected old list format in live_trades.json. Normalizing...")
                trade_data = {today: {"trades": raw_data}}

            # ✅ Case 2: New dict format
            elif isinstance(raw_data, dict):
                trade_data = raw_data
                if today not in trade_data:
                    trade_data[today] = {"trades": []}

            else:
                print("⚠️ Unrecognized format. Resetting trade data.")
                trade_data = {today: {"trades": []}}

        except Exception as e:
            print(f"⚠️ Error loading live_trades.json: {e}")
            trade_data = {today: {"trades": []}}

else:
    trade_data = {today: {"trades": []}}

# ✅ Ensure structure
if not isinstance(trade_data, dict) or today not in trade_data or "trades" not in trade_data[today]:
    trade_data = {today: {"trades": []}}

# ✅ Extract existing trade keys for deduplication
existing_keys = {
    (t["symbol"], t["datetime"]) for t in trade_data[today]["trades"]
}

results = []
new_trades = []

for symbol in SYMBOLS:
    try:
        symbol_id = get_symbol_id(symbol)
        df = get_price_data(symbol_id, days=1, interval="OneMinute")
        if df is None or df.empty:
            print(f"⚠️ No data for {symbol}")
            continue

        df = add_technical_indicators(df)
        signals_df = get_strategy_signals(symbol, df, min_confidence=CONFIDENCE_THRESHOLD, debug=True)
        print(f"📈 Found {len(signals_df)} signals for {symbol}. Columns: {signals_df.columns.tolist() if not signals_df.empty else 'N/A'}")
        if signals_df is None or signals_df.empty:
            print(f"🚫 No signals for {symbol}")
            continue

        valid_signals = signals_df[signals_df["signal"] == True].copy()
        if 'datetime' not in valid_signals.columns:
            valid_signals.reset_index(inplace=True)
        if 'datetime' not in valid_signals.columns:
            print(f"❌ Still missing 'datetime' in {symbol}. Columns: {valid_signals.columns}")
            continue

        print(f"📊 Valid signals for {symbol}:", valid_signals[["datetime", "confidence", "strategies", "signal"]])

        for _, signal in valid_signals.iterrows():
            entry_time = pd.to_datetime(signal["datetime"])
            entry_idx = df.index.get_indexer([entry_time], method="nearest")[0]
            entry_price = df.loc[df.index[entry_idx], "close"]

            exit_price = None
            exit_time = None
            exit_reason = "timer"

            for i in range(entry_idx + 1, min(len(df), entry_idx + 60)):
                current_time = df.index[i]
                current_price = df.loc[current_time, "close"]
                change_pct = (current_price - entry_price) / entry_price

                if change_pct >= TARGET_GAIN:
                    exit_price = current_price
                    exit_time = current_time
                    exit_reason = "target"
                    break
                elif change_pct <= STOP_LOSS:
                    exit_price = current_price
                    exit_time = current_time
                    exit_reason = "stop"
                    break

            if exit_price is None:
                timeout_idx = min(len(df) - 1, entry_idx + EXIT_HOLD_MINUTES)
                exit_time = df.index[timeout_idx]
                exit_price = df.loc[exit_time, "close"]
                exit_reason = "timer"

            quantity = int(wallet["capital"] // entry_price)
            if quantity == 0:
                print(f"⚠️ Insufficient capital to buy {symbol} at {entry_price}")
                continue

            cost = entry_price * quantity
            proceeds = exit_price * quantity

            wallet["capital"] -= cost
            wallet["capital"] += proceeds

            result = {
                "symbol": symbol,
                "datetime": entry_time.isoformat(),
                "signal": signal.get("signal", "none"),
                "confidence": signal.get("confidence", 0),
                "strategies_used": ",".join(signal.get("strategies", [])),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "exit_time": exit_time.isoformat(),
                "exit_reason": exit_reason,
                "pnl": proceeds - cost,
                "pnl_pct": (exit_price - entry_price) / entry_price * 100,
                "quantity": quantity,
                "capital_after": wallet["capital"]
            }
            key = (symbol, result["datetime"])
            if key not in existing_keys:
                results.append(result)
                new_trades.append(result)
                print(f"✅ Appended simulated trade for {symbol} at {entry_time} → exit: {exit_time} ({exit_reason})")

    except Exception as e:
        print(f"❌ Error simulating {symbol}: {e}")

# Save results
if results:
    df_results = pd.DataFrame(results)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    df_results.to_csv(OUTPUT_PATH, index=False)
    print(f"✅ Simulated results saved to {OUTPUT_PATH}")

    trade_data[today]["trades"].extend(new_trades)

    with open(TRADES_JSON, "w") as f:
        json.dump(trade_data, f, indent=2)
    print(f"📦 Saved trades to {TRADES_JSON}")

    save_wallet(wallet)
    print(f"💰 Updated capital: {wallet['capital']:.2f} saved to {WALLET_PATH}")
else:
    print("🚫 No trades simulated.")
