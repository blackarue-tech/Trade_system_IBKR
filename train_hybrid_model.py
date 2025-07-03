# train_hybrid_model.py - Hybrid RandomForest model trainer for AAPL, MSFT, NVDA, TSLA

import os
import pandas as pd
from datetime import datetime, timedelta
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report
import joblib
from modules.model_utils import generate_training_data
from modules.price_data import get_price_data
from modules.questrade import get_symbol_id

SYMBOLS = ["MSTR", "HOOD", "NVDA", "AMZN"]
DAYS = 180  # Lookback for training data
MODEL_PATH = "exports/stock_selector_model.pkl"

all_data = []
for symbol in SYMBOLS:
    print(f"📥 Fetching data for {symbol}")
    try:
        symbol_id = get_symbol_id(symbol,"C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json")  # Ensure this function is defined in your modules
        df = get_price_data(symbol_id, days=DAYS)  # Adjust symbol_id fetch if needed
        if df is None or df.empty:
            print(f"⚠️ No data for {symbol}")
            continue
        df = generate_training_data(df)
        df["symbol"] = symbol
        all_data.append(df)
    except Exception as e:
        print(f"❌ Error fetching data for {symbol}: {e}")

if not all_data:
    print("❌ No data collected, exiting.")
    exit()

data = pd.concat(all_data)
features = [col for col in data.columns if col not in ["label", "future_return", "symbol"]]
X = data[features].fillna(0)
y = data["label"]

print("🧪 Splitting data...")
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, shuffle=False)

print("🌲 Training RandomForest model...")
model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
model.fit(X_train, y_train)

print("✅ Model trained. Evaluating...")
y_pred = model.predict(X_test)
print(classification_report(y_test, y_pred))
print(f"Accuracy: {accuracy_score(y_test, y_pred):.4f}")

joblib.dump(model, MODEL_PATH)
print(f"💾 Model saved to {MODEL_PATH}")
