# train_hybrid_model.py - Dynamic symbol alignment with live-traded pipeline

import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score
from sklearn.utils import class_weight
from datetime import datetime
from pytz import timezone as pytz_timezone
import joblib
import json

from modules.price_data import get_price_data
from modules.model_utils import add_technical_indicators, extract_features, FEATURE_COLUMNS
from modules.log_utils import log, fail_safe

LOCAL_TZ = pytz_timezone("America/Toronto")
now = datetime.now(LOCAL_TZ)
date_tag = now.strftime("%Y%m%d_%H%M")

# === CONFIG ===
EXPORT_MODEL_PATH = "exports/stock_selector_model.pkl"
DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "MSTR", "HOOD"]
TOP_GAINERS_JSON = "exports/top_gainers.json"

def load_training_symbols(json_path=TOP_GAINERS_JSON, fallback=DEFAULT_SYMBOLS, limit=10):
    try:
        if not os.path.exists(json_path):
            log(f"{json_path} not found. Using fallback SYMBOLS.", "WARNING", "TrainHybrid")
            return fallback

        with open(json_path) as f:
            data = json.load(f)

        symbols = [s["symbol"] for s in data.get("symbols", []) if isinstance(s, dict)]
        if not symbols:
            log("No symbols in top_gainers.json, using fallback SYMBOLS.", "WARNING", "TrainHybrid")
            return fallback

        symbols = list(dict.fromkeys(symbols))  # deduplicate while preserving order
        return symbols[:limit]
    except Exception as e:
        log(f"Error loading training symbols: {e}. Using fallback SYMBOLS.", "ERROR", "TrainHybrid")
        return fallback

symbols_to_train = load_training_symbols()

all_data = []

for symbol in symbols_to_train:
    try:
        log(f"Fetching data for {symbol}", "INFO", "TrainHybrid")
        df = get_price_data(symbol, days=90, interval="minute")
        if df is None or df.empty:
            log(f"No data for {symbol}", "WARNING", "TrainHybrid")
            continue
        df = add_technical_indicators(df)
        features_df = extract_features(df)
        future_close = df["close"].shift(-5)
        target = (future_close > df["close"] * 1.003).astype(int)
        features_df["target"] = target
        all_data.append(features_df)
    except Exception as e:
        log(f"Error processing {symbol}: {e}", "ERROR", "TrainHybrid")

if not all_data:
    log("❌ No data collected for training. Exiting.", "ERROR", "TrainHybrid")
    exit()

try:
    full_df = pd.concat(all_data).dropna().reset_index(drop=True)
    log(f"✅ Data collected: {len(full_df)} rows from {len(symbols_to_train)} symbols.", "INFO", "TrainHybrid")

    X = full_df[FEATURE_COLUMNS]
    y = full_df["target"]

    weights = class_weight.compute_sample_weight(class_weight="balanced", y=y)
    X_train, X_test, y_train, y_test, w_train, w_test = train_test_split(
        X, y, weights, test_size=0.3, random_state=42
    )

    log("Training RandomForest model...", "INFO", "TrainHybrid")
    model = RandomForestClassifier(n_estimators=100, max_depth=10, random_state=42)
    model.fit(X_train, y_train, sample_weight=w_train)

    log("Model trained. Evaluating...", "INFO", "TrainHybrid")
    y_pred = model.predict(X_test)
    report = classification_report(y_test, y_pred)
    acc = accuracy_score(y_test, y_pred)
    log(f"\n{report}\nAccuracy: {acc:.4f}", "INFO", "TrainHybrid")

    joblib.dump(model, EXPORT_MODEL_PATH)
    log(f"✅ Model saved to {EXPORT_MODEL_PATH}", "INFO", "TrainHybrid")

    importances = model.feature_importances_
    feature_names = model.feature_names_in_
    importance_df = pd.DataFrame({
        "feature": feature_names,
        "importance": importances
    }).sort_values(by="importance", ascending=False)

    log(f"Top Feature Importances:\n{importance_df}", "INFO", "TrainHybrid")

    os.makedirs("exports", exist_ok=True)
    csv_path = f"exports/feature_importances_{date_tag}.csv"
    importance_df.to_csv(csv_path, index=False)
    log(f"Feature importances snapshot saved to {csv_path}", "INFO", "TrainHybrid")

    master_log_path = "exports/feature_importances_log.csv"
    if os.path.exists(master_log_path):
        master_df = pd.read_csv(master_log_path)
    else:
        master_df = pd.DataFrame()

    importance_df["date_tag"] = date_tag
    master_df = pd.concat([master_df, importance_df], ignore_index=True)
    master_df.to_csv(master_log_path, index=False)
    log(f"Appended feature importances to {master_log_path}", "INFO", "TrainHybrid")

    plt.figure(figsize=(10, 6))
    plt.barh(importance_df["feature"], importance_df["importance"], color="skyblue")
    plt.gca().invert_yaxis()
    plt.xlabel("Importance")
    plt.title(f"Feature Importances - {date_tag}")
    plt.tight_layout()
    plot_path = f"exports/feature_importances_{date_tag}.png"
    plt.savefig(plot_path, dpi=300)
    plt.show()
    log(f"Feature importances plot saved to {plot_path}", "INFO", "TrainHybrid")

except Exception as e:
    log(f"❌ Training pipeline failed: {e}", "ERROR", "TrainHybrid")
