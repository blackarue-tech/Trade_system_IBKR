
import pandas as pd
import numpy as np

def sigmoid(x):
    return 1 / (1 + np.exp(-x))

def get_strategy_signals(symbol, df, min_confidence=0.0, debug=False):
    signals = []
    for i in range(1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]
        log = []

        confidence = 0.0
        strategies_used = []

        # RSI scoring
        if row.get("RSI") is not None:
            if row["RSI"] < 45:
                rsi_score = (45 - row["RSI"]) / 60
                confidence += rsi_score
                strategies_used.append("RSI")
                log.append(f"RSI={row['RSI']:.2f}, score={rsi_score:.2f}")

        # MA crossover
        if row.get("MA_fast") is not None and row.get("MA_slow") is not None:
            ma_diff = row["MA_fast"] - row["MA_slow"]
            slope_score = sigmoid(ma_diff * 5) - 0.5
            confidence += slope_score
            strategies_used.append("MA")
            log.append(f"MA_fast={row['MA_fast']:.2f}, MA_slow={row['MA_slow']:.2f}, score={slope_score:.2f}")

        # Breakout logic
        if i >= 20:
            recent_high = df["high"].iloc[i - 20:i].max()
            if row["close"] > recent_high * 1.01:
                confidence += 0.3
                strategies_used.append("Breakout")
                log.append(f"Breakout over {recent_high:.2f}")

        # Context modifiers
        if row.get("volatility") and row["volatility"] > 0.03:
            confidence *= 0.9
            log.append(f"High volatility: {row['volatility']:.2f} → confidence *= 0.9")

        time = row.name if isinstance(row.name, pd.Timestamp) else None
        if time and time.hour in [10, 11, 14, 15]:
            confidence *= 1.1
            log.append(f"Favorable time {time.hour}h → confidence *= 1.1")

        confidence = round(min(confidence, 1.0), 3)

        # Determine signal type
        signal_type = None
        if "RSI" in strategies_used and row["RSI"] < 30:
            signal_type = "buy"
        elif "RSI" in strategies_used and row["RSI"] > 70:
            signal_type = "sell"
        elif "Breakout" in strategies_used:
            signal_type = "buy"

        if debug:
            print(f"🔍 {symbol} @ {row.name} → Conf={confidence:.2f}, Strat={strategies_used}, Signal={signal_type}, Log={log}")

        if signal_type and confidence >= min_confidence:
            signals.append({
                "timestamp": row.name,
                "symbol": symbol,
                "signal": signal_type,
                "confidence": confidence,
                "strategies": strategies_used
            })

    return pd.DataFrame(signals)
