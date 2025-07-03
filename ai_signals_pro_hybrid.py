import pandas as pd
import joblib
from modules.model_utils import add_technical_indicators

MODEL_PATH = "exports/stock_selector_model.pkl"
try:
    MODEL = joblib.load(MODEL_PATH)
    print("✅ Hybrid RandomForest model loaded for signal scoring.")
except:
    MODEL = None
    print("⚠️ Hybrid model not found, using TA-only scoring.")

def get_hybrid_signal_score(df):
    df = add_technical_indicators(df)
    if MODEL:
        features = MODEL.feature_names_in_
        X = df[features].fillna(0)
        preds = MODEL.predict_proba(X)[:, 1]
        df["ml_score"] = preds
    else:
        df["ml_score"] = 0.0
    return df

def compute_hybrid_confidence(ta_confidence, ml_score):
    return 0.6 * ta_confidence + 0.4 * ml_score
