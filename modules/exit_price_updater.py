# exit_price_updater.py

import pandas as pd
from datetime import datetime
from utils.trade_utils import load_live_trades, save_live_trades
from questrade_api import get_access_token, get_quote
from utils.price_data import get_price_data
from utils.model_utils import add_technical_indicators, apply_context_penalty
from ai_signals import get_strategy_signals
from utils.Wallet_Utils import credit_wallet_balance  # 💳 wallet integration

EXIT_RULES = {
    "profit_threshold_pct": 1.0,
    "loss_threshold_pct": -1.0,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
}

WALLET_FILE = "exports/wallet.json"

def apply_exit_rules(trade):
    symbol = trade["symbol"]
    symbol_id = trade["symbolId"]
    entry_price = trade["entry_price"]
    shares = trade["shares"]

    df = get_price_data(symbol_id, days=1, interval="OneMinute")
    if df is None or df.empty:
        return None

    df = add_technical_indicators(df)
    last = df.iloc[-1]
    price = float(last["close"])
    rsi = last.get("rsi")

    pnl_pct = ((price - entry_price) / entry_price) * 100

    if pnl_pct >= EXIT_RULES["profit_threshold_pct"]:
        return price, "take_profit"
    elif pnl_pct <= EXIT_RULES["loss_threshold_pct"]:
        return price, "stop_loss"
    elif rsi and rsi > EXIT_RULES["rsi_overbought"]:
        return price, "rsi_overbought"
    elif rsi and rsi < EXIT_RULES["rsi_oversold"]:
        return price, "rsi_oversold"

    return None

def update_exit_price(trade, symbol_id):
    if trade.get("exit_updated"):
        return False

    symbol = trade["symbol"]
    entry_price = trade["entry_price"]
    shares = trade["shares"]
    entry_time = pd.to_datetime(trade["entry_time"])
    now = datetime.now()

    # --- Live quote fallback ---
    access_token, api_server = get_access_token()
    quote = get_quote(symbol, symbol_id, access_token, api_server)
    current_price = quote.get("bid") or quote.get("last")
    price_source = "quote"

    # Fallback to candle close if needed
    if not current_price:
        df_live = get_price_data(symbol_id, days=1, interval="OneMinute")
        if df_live is not None and not df_live.empty:
            current_price = float(df_live["close"].iloc[-1])
            price_source = "candle_close"
            print(f"✅ Fallback candle used for {symbol}: {current_price}")
        else:
            print(f"❌ No price available for {symbol}")
            return False

    pnl = (current_price - entry_price) * shares
    pnl_pct = ((current_price - entry_price) / entry_price) * 100

    # Load context for exit tuning
    df_context = get_price_data(symbol_id, days=30, interval="OneDay")
    df_context = add_technical_indicators(df_context) if df_context is not None else None
    last_row = df_context.iloc[-1] if df_context is not None and not df_context.empty else {}

    rsi = last_row.get("rsi", 50)
    volatility = last_row.get("volatility", 1.0)
    macd_hist = last_row.get("macd_hist", 0.0)

    # Apply context penalty logic
    confidence = apply_context_penalty("Exit_Adjustment", rsi, volatility, macd_hist, 1.0)
    adjusted_profit_pct = EXIT_RULES["profit_threshold_pct"] * confidence
    adjusted_loss_pct = EXIT_RULES["loss_threshold_pct"] * confidence

    # --- Rolling trailing stop logic (new) ---
    df_short = get_price_data(symbol_id, days=1, interval="OneMinute")
    df_short = get_price_data(symbol_id, days=1, interval="OneMinute")
    if df_short is not None and not df_short.empty:
        df_short.index = pd.to_datetime(df_short.index)  # ✅ fix: ensure datetime index
        df_short = df_short[df_short.index >= entry_time]
        if not df_short.empty:
            max_price = df_short["high"].max()
            trailing_stop_pct = -0.75  # allow 0.75% drop from peak
            drawdown_pct = ((current_price - max_price) / max_price) * 100
            if drawdown_pct <= trailing_stop_pct:
                reason = "trailing_stop"
            else:
                reason = None
        else:
            reason = None
    else:
        reason = None

    # --- Primary exit triggers ---
    if pnl_pct >= adjusted_profit_pct:
        reason = "take_profit"
    elif pnl_pct <= adjusted_loss_pct:
        reason = "stop_loss"
    elif rsi > EXIT_RULES["rsi_overbought"]:
        reason = "rsi_overbought"
    elif rsi < EXIT_RULES["rsi_oversold"]:
        reason = "rsi_oversold"
    elif (now - entry_time).total_seconds() > 3600:  # 60 min timeout
        reason = "timeout"

    if not reason:
        return False

    # --- Apply exit ---
    trade["exit_price"] = round(current_price, 2)
    trade["pnl"] = round(pnl, 2)
    trade["pnl_pct"] = round(pnl_pct, 2)
    trade["exit_time"] = now.isoformat()
    trade["holding_minutes"] = int((now - entry_time).total_seconds() / 60)
    trade["exit_reason"] = reason
    trade["exit_price_source"] = price_source
    trade["exit_updated"] = True

    credit_wallet_balance(WALLET_FILE, trade["capital_used"])
    print(f"📤 Exit triggered for {symbol} - {reason.upper()}, PnL: {pnl:.2f}, using {price_source}")
    return True