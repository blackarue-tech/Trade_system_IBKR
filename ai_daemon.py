import json
import os
from datetime import datetime
from utils import save_json, load_json, log
from analyze_exit_failures import run_exit_failure_analysis

EXIT_FAILURE_ANALYSIS_PATH = "exports/exit_failure_analysis.json"
EXIT_PARAMS_PATH = "inputs/ai_strategy_configs/exit_params.json"
LIVE_PARAMS_PATH = "inputs/ai_strategy_configs/live_params.json"
TRADES_PATH = "exports/live_trades.json"


def adaptive_tune():
    trades = load_json(TRADES_PATH)
    live_params = load_json(LIVE_PARAMS_PATH)
    exit_params = load_json(EXIT_PARAMS_PATH)

    # === Compute win rate, avg_pnl, avg_duration ===
    summary = {}
    for day, data in trades.items():
        for trade in data.get("trades", []):
            strategies = trade.get("strategies_used", [])
            if not strategies:
                continue
            strat = strategies[0]
            pnl = trade.get("pnl", 0)
            duration = (datetime.fromisoformat(trade.get("exit_time", datetime.now().isoformat())) - datetime.fromisoformat(trade["datetime"])).total_seconds() / 60
            summary.setdefault(strat, {"wins": 0, "losses": 0, "pnl_list": [], "durations": [], "confidences": []})
            if pnl > 0:
                summary[strat]["wins"] += 1
            else:
                summary[strat]["losses"] += 1
            summary[strat]["pnl_list"].append(pnl)
            summary[strat]["durations"].append(duration)
            if "confidence" in trade:
                summary[strat]["confidences"].append(trade["confidence"])
    
    run_exit_failure_analysis()
    # === Tuning logic ===
    reasons_log = {}
    updated = False
    strategy_weights = live_params.get("strategy_weights", {})

    for strat, stats in summary.items():
        total_trades = stats["wins"] + stats["losses"]
        win_rate = stats["wins"] / total_trades if total_trades else 0
        avg_pnl = sum(stats["pnl_list"]) / total_trades if total_trades else 0
        avg_duration = sum(stats["durations"]) / total_trades if total_trades else 0
        avg_conf = sum(stats["confidences"]) / len(stats["confidences"]) if stats["confidences"] else None
        weight = strategy_weights.get(strat, 1.0)

        reason = "Stable"

        # Adjust weights
        if win_rate > 0.6:
            new_weight = min(weight + 0.05, 1.5)
            reason = "Win rate high"
        elif win_rate < 0.4:
            new_weight = max(weight - 0.05, 0.5)
            reason = "Win rate low"
        else:
            new_weight = weight

        if new_weight != weight:
            strategy_weights[strat] = round(new_weight, 2)
            updated = True
            log(f"⚖️ {strat}: Adjusted weight from {weight} to {new_weight} due to {reason} (win_rate={win_rate:.2f})", "INFO", "AIDaemon")

        # Tune exit_params based on exit performance
        strat_exit = exit_params.get(strat, {})
        old_hold = strat_exit.get("adaptive_hold_minutes", 180)
        new_hold = old_hold

        if avg_duration > old_hold * 1.2:
            new_hold = int(old_hold * 0.9)
            reason_exit = "Held too long"
        elif avg_duration < old_hold * 0.5:
            new_hold = int(old_hold * 1.1)
            reason_exit = "Exiting too fast"
        else:
            reason_exit = "Hold time stable"

        if new_hold != old_hold:
            strat_exit["adaptive_hold_minutes"] = new_hold
            exit_params[strat] = strat_exit
            updated = True
            log(f"⏱️ {strat}: Adjusted hold from {old_hold} to {new_hold} due to {reason_exit} (avg_duration={avg_duration:.1f}m)", "INFO", "AIDaemon")
            reasons_log[strat] = reason_exit

    # === Adjust min_confidence ===
    avg_win_rate = sum(stats["wins"] / (stats["wins"] + stats["losses"] + 1e-5) for stats in summary.values()) / max(len(summary), 1)
    min_conf = live_params.get("min_confidence", 0.5)

    if avg_win_rate > 0.55:
        new_conf = min(min_conf + 0.02, 0.8)
        reason_conf = "System win rate high"
    elif avg_win_rate < 0.4:
        new_conf = max(min_conf - 0.02, 0.4)
        reason_conf = "System win rate low"
    else:
        new_conf = min_conf
        reason_conf = "System stable"

    if new_conf != min_conf:
        live_params["min_confidence"] = round(new_conf, 3)
        updated = True
        log(f"🔧 min_confidence adjusted from {min_conf} to {new_conf} due to {reason_conf}", "INFO", "AIDaemon")

    # === Adaptive Exit Signal and Buy Filter Tuning ===
    if os.path.exists(EXIT_FAILURE_ANALYSIS_PATH):
        with open(EXIT_FAILURE_ANALYSIS_PATH) as f:
            analysis = json.load(f)
        reasons_by_strategy = analysis.get("reasons_by_strategy", {})

        for strategy, reasons in reasons_by_strategy.items():
            existing_signals = set(exit_params.get(strategy, {}).get("exit_signals", []))
            proposed = []

            for reason, count in reasons:
                if reason in ["rsi_drop", "vwap_reversion", "macd_bearish", "bollinger_breakdown", "atr_spike"]:
                    if count > 5 and reason not in existing_signals:
                        proposed.append(reason)

            if proposed:
                exit_params.setdefault(strategy, {}).setdefault("exit_signals", []).extend(proposed)
                log(f"🧠 Strategy {strategy} updated: added exit_signals {proposed} due to frequent failures.", "INFO", "AIDaemon")
                updated = True

        # Tuning Buy Filters
        if "symbols_failed_post_entry" in analysis:
            failures = analysis["symbols_failed_post_entry"]
            filter_tweaks = {}
            for entry in failures:
                symbol = entry["symbol"]
                reason = entry.get("reason", "unknown")
                strategy = entry.get("strategy", "Momentum")
                confidence = entry.get("confidence", 0)

                if confidence < 0.45:
                    filter_tweaks.setdefault(strategy, {"low_conf": 0, "count": 0})
                    filter_tweaks[strategy]["low_conf"] += 1
                    filter_tweaks[strategy]["count"] += 1

            for strategy, data in filter_tweaks.items():
                low_conf_ratio = data["low_conf"] / data["count"]
                if low_conf_ratio > 0.5:
                    prev = live_params.get("min_confidence", 0.5)
                    new_val = round(min(prev + 0.02, 0.8), 3)
                    live_params["min_confidence"] = new_val
                    log(f"🔍 Buy filter updated: min_confidence raised to {new_val} due to frequent low-confidence failures in {strategy}", "INFO", "AIDaemon")
                    updated = True

        # === Sandbox Evaluation ===
        if "sandbox_failures" in analysis:
            sandbox_stats = analysis["sandbox_failures"]
            total = sandbox_stats.get("tested", 0)
            failed = sandbox_stats.get("failed", 0)
            fail_ratio = failed / total if total > 0 else 0
            if fail_ratio > 0.4:
                prev = live_params.get("min_confidence", 0.5)
                new_val = round(min(prev + 0.02, 0.8), 3)
                live_params["min_confidence"] = new_val
                log(f"🧪 Sandbox tuning: raised min_confidence to {new_val} due to {fail_ratio:.2%} failure rate in sandbox-passed entries.", "INFO", "AIDaemon")
                updated = True

    if updated:
        save_json(live_params, LIVE_PARAMS_PATH)
        save_json(exit_params, EXIT_PARAMS_PATH)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"✅ AI Daemon tuning completed at {now_str}", "INFO", "AIDaemon")
    if reasons_log:
        log(f"Reasons for adjustments: {json.dumps(reasons_log, indent=2)}", "INFO", "AIDaemon")


if __name__ == "__main__":
    adaptive_tune()
