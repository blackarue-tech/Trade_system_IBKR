# exit_executor_configured.py – Refactored with CONFIG-based exit signal logic
import os, json, time
from datetime import datetime, timedelta
import pytz
from dateutil.parser import isoparse
import pandas as pd
from utils import atomic_save_json, update_system_status
from modules.questrade import get_quote
from modules.price_data import get_price_data
from modules.Wallet_Utils import credit_wallet_balance
from modules.model_utils import add_technical_indicators

def load_strategy_config(path):
    with open(path) as f:
        return json.load(f)
LIVE_TRADES_PATH = "exports/live_trades.json"
WALLET_PATH = "exports/wallet.json"
CONFIG_PATH = "inputs/ai_strategy_configs/exit_params.json"
CONFIG = load_strategy_config(CONFIG_PATH)
EXIT_RULES = CONFIG  # ✅ correct


LOOP_INTERVAL = CONFIG.get("loop_interval_sec", 30)
LOCAL_TZ = pytz.timezone("America/Toronto")

def get_market_now():
    return datetime.now(LOCAL_TZ)

def load_live_trades():
    if os.path.exists(LIVE_TRADES_PATH):
        with open(LIVE_TRADES_PATH, "r") as f:
            return json.load(f)
    return {}

def save_live_trades(trades):
    atomic_save_json(trades, LIVE_TRADES_PATH)

def get_primary_strategy(trade):
    return trade.get("strategies_used", ["Unknown"])[0]

def get_rule(trade, key, default):
    strategy = get_primary_strategy(trade)
    return EXIT_RULES.get(strategy, {}).get(key, default)

def strategy_exit_check(trade, current_price):
    symbol_id = trade["symbolId"]
    df = get_price_data(symbol_id, days=1, interval="OneMinute")
    if df is None or df.empty:
        return None, None
    df = add_technical_indicators(df)
    latest = df.iloc[-1]

    signals = get_rule(trade, "exit_signals", [])

    if "vwap_reversion" in signals and latest["close"] < latest["vwap"]:
        return "vwap_reversion", current_price
    if "rsi_drop" in signals and latest.get("rsi", 100) < 50:
        return "rsi_drop", current_price
    if "breakout_fail" in signals and latest["close"] < trade["entry_price"]:
        return "breakout_fail", current_price
    if "macd_bearish" in signals and latest.get("macd_hist", 0) < 0:
        return "macd_bearish", current_price
    if "ma_breakdown" in signals and latest["close"] < latest.get("ema20", latest["close"]):
        return "ma_breakdown", current_price
    print(f"📉 {signals} signal check for {trade['symbol']}: "
      f"close={latest['close']:.2f}, vwap={latest.get('vwap',0):.2f}, "
      f"rsi={latest.get('rsi',0):.2f}, macd_hist={latest.get('macd_hist',0):.4f}")
    return None, None

def trailing_stop_check(trade, current_price):
    trail_pct = get_rule(trade, "trailing_pct", 0.01)
    peak = trade.get("peak_price", trade["entry_price"])
    peak = max(peak, current_price)
    trade["peak_price"] = peak
    if current_price < peak * (1 - trail_pct):
        return "trailing_stop", current_price
    return None, None

def get_adaptive_hold_time(trade):
    default_hold = CONFIG.get("default_hold_minutes", 240)
    return get_rule(trade, "adaptive_hold_minutes", default_hold)

def check_exit_conditions(trade, current_price):
    entry_price = trade["entry_price"]
    entry_time = isoparse(trade["datetime"])
    now = get_market_now()
    time_held_sec = (now - entry_time).total_seconds()
    pnl_pct = (current_price - entry_price) / entry_price

    strategy = trade["strategies_used"][0]
    early_rules = CONFIG.get("early_exit_rules", {}).get(strategy, {})

    if early_rules:
        if pnl_pct >= early_rules.get("profit_pct", 0.0025) and time_held_sec >= early_rules.get("min_secs", 300):
            return "early_profit_capture", current_price
        if pnl_pct <= early_rules.get("loss_pct", -0.002) and time_held_sec >= early_rules.get("loss_wait_secs", 600):
            return "smart_cut_loss", current_price

    strat_reason, strat_exit = strategy_exit_check(trade, current_price)
    if strat_exit:
        return strat_reason, strat_exit

    trail_reason, trail_exit = trailing_stop_check(trade, current_price)
    if trail_exit:
        return trail_reason, trail_exit

    adaptive_hold_min = get_adaptive_hold_time(trade)
    if time_held_sec >= adaptive_hold_min * 60:
        return "adaptive_timer", current_price

    target_gain = get_rule(trade, "target_gain", CONFIG.get("default_target_gain", 0.015))
    stop_loss = get_rule(trade, "stop_loss", CONFIG.get("default_stop_loss", -0.015))

    if pnl_pct >= target_gain:
        return "target", current_price
    elif pnl_pct <= stop_loss:
        return "stop", current_price

    return None, None

def exit_executor_loop():
    try:
        while True:
            print("🔄 Checking open trades...")
            trades = load_live_trades()
            today = get_market_now().strftime("%Y-%m-%d")
            updated = False

            if today not in trades:
                time.sleep(LOOP_INTERVAL)
                continue

            for trade in trades[today]["trades"]:
                if trade.get("exit_price"):
                    continue

                symbol = trade["symbol"]
                symbol_id = trade["symbolId"]
                quantity = trade["shares"]
                print(f"🔍 Checking {symbol} with symbolId: {symbol_id}")

                try:
                    quote = get_quote(symbol_id)
                except Exception as e:
                    print(f"⚠️ Quote fetch failed for {symbol}: {e}")
                    continue

                current_price = (
                    quote.get("last") or
                    quote.get("lastTradePrice") or
                    quote.get("lastTradePriceTrHrs") or
                    quote.get("bidPrice")
                )

                if current_price is None:
                    print(f"⚠️ No valid price for {symbol}, skipping...")
                    continue

                exit_reason, exit_price = check_exit_conditions(trade, current_price)

                if exit_price:
                    entry_price = trade["entry_price"]
                    trade["exit_price"] = exit_price
                    trade["exit_time"] = get_market_now().isoformat()
                    trade["exit_reason"] = exit_reason
                    trade["pnl"] = (exit_price - entry_price) * quantity
                    trade["pnl_pct"] = (exit_price - entry_price) / entry_price * 100
                    trade["status"] = "closed"
                    credit_wallet_balance(WALLET_PATH, exit_price * quantity)
                    updated = True

                    print(f"✅ Closed {symbol} - {exit_reason} @ {exit_price:.2f} | PnL: {trade['pnl']:.2f}")

            if updated:
                save_live_trades(trades)
            # Define closed and open_trades for status update
            all_trades = trades.get(today, {}).get("trades", [])
            closed = [t for t in all_trades if t.get("status") == "closed"]
            open_trades = [t for t in all_trades if t.get("status") != "closed"]
            update_system_status("exit_executor", {
                "trades_closed": len(closed),
                "remaining_open": len(open_trades)
            })
            time.sleep(LOOP_INTERVAL)
    except KeyboardInterrupt:
        print("🛑 Stopped live_executor loop.")
    except Exception as e:
        print(f"❌ Unexpected error in loop: {e}")
        time.sleep(LOOP_INTERVAL)
if __name__ == "__main__":
    exit_executor_loop()
