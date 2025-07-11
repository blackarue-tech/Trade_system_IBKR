# trade_watcher.py

import os
import json
from datetime import datetime

LIVE_TRADES_PATH = "exports/live_trades.json"
TOP_SYMBOLS_PATH = "exports/top_confidence_signals.json"
OPTIMIZED_RULES_PATH = "exports/optimized_rules.json"
AVOID_RULES_PATH = "exports/avoid_conditions.json"

def load_json(path, default=None):
    if not os.path.exists(path):
        print(f"⚠️ File not found: {path}. Creating default...")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(default or {}, f, indent=2)
        return default or {}
    with open(path) as f:
        return json.load(f)

def matches_rule(context, rule):
    try:
        return (
            context.get("time_bucket") == rule.get("time_bucket") and
            context.get("volatility_band") == rule.get("volatility_band") and
            float(context.get("rsi", 0)) >= rule.get("rsi_min", 0) and
            float(context.get("rsi", 0)) <= rule.get("rsi_max", 100) and
            float(context.get("bb_width", 0)) >= rule.get("bb_width_min", 0)
        )
    except Exception:
        return False

def time_bucket_from_hour(hour):
    if hour < 11:
        return "morning"
    elif hour < 14:
        return "midday"
    elif hour < 16:
        return "afternoon"
    else:
        return "late"

def enrich_trade_context(trade):
    ts = datetime.fromisoformat(trade["entry_time"])
    trade["time_bucket"] = time_bucket_from_hour(ts.hour)
    return trade

def watch_trades():
    live_trades = load_json(LIVE_TRADES_PATH, {"trades": []}).get("trades", [])
    top_symbols = [x["symbol"] for x in load_json(TOP_SYMBOLS_PATH, [])]
    rules = load_json(OPTIMIZED_RULES_PATH, {"recommended_rules": []}).get("recommended_rules", [])
    avoid = load_json(AVOID_RULES_PATH, {"avoid_conditions": []}).get("avoid_conditions", [])

    print(f"🔎 Watching trades for top symbols: {', '.join(top_symbols)}\\n")

    for trade in live_trades:
        if trade["symbol"] not in top_symbols:
            continue

        context = trade.get("context_features", {})
        if not context or not isinstance(context, dict):
            print(f"❓ {trade['symbol']} has no context available")
            continue

        enriched = enrich_trade_context(trade)
        context["time_bucket"] = enriched["time_bucket"]
        context["volatility_band"] = trade.get("volatility_band", "unknown")

        matched_rule = next((r for r in rules if matches_rule(context, r)), None)
        matched_avoid = next((r for r in avoid if matches_rule(context, r)), None)

        if matched_avoid:
            print(f"⚠️ {trade['symbol']} violates avoid rule: {matched_avoid}")
        elif matched_rule:
            print(f"✅ {trade['symbol']} matches rule: {matched_rule}")
        else:
            print(f"❓ {trade['symbol']} did not match any rule or avoid condition")

if __name__ == "__main__":
    watch_trades()
