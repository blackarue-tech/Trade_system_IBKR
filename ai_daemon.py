# ai_daemon.py – Adaptive AI Engine to Analyze Trades and Refine Strategy

import os, json, time
from datetime import datetime, timedelta
import pytz
from utils import atomic_save_json, update_system_status
LIVE_TRADES_PATH = "exports/live_trades.json"
EXIT_CONFIG_PATH = "inputs/ai_strategy_configs/exit_params.json"
LIVE_CONFIG_PATH = "inputs/ai_strategy_configs/live_params.json"
AI_LOG_PATH = "exports/ai_decisions_log.json"
PENALTY_TRACKER_PATH = "exports/penalty_tracker.json"
LOOP_INTERVAL = 120
TZ = pytz.timezone("America/New_York")

# --------- File Utils ----------
def load_config(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_config(config, path):
        atomic_save_json(config, path)

def load_live_trades():
    if os.path.exists(LIVE_TRADES_PATH):
        with open(LIVE_TRADES_PATH, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_json(data, path):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def log_ai_decision(entry):
    log_data = load_json(AI_LOG_PATH)
    if not isinstance(log_data, list):
        log_data = []
    log_data.append(entry)
    save_json(log_data, AI_LOG_PATH)

# --------- Analysis Logic ----------
def analyze_trades(trades_by_day):
    summary = {}
    for date, data in trades_by_day.items():
        for trade in data.get("trades", []):
            if trade.get("status") != "closed":
                continue
            if trade.get("exit_reason") in ["stop", "timer", "adaptive_timer"]:
                continue

            strategy = trade.get("strategies_used", ["Unknown"])[0]
            pnl = trade.get("pnl", 0)
            try:
                entry = datetime.fromisoformat(trade["datetime"]).astimezone(TZ)
                exit_ = datetime.fromisoformat(trade["exit_time"]).astimezone(TZ)
            except Exception:
                continue

            duration = (exit_ - entry).total_seconds() / 60
            if strategy not in summary:
                summary[strategy] = {
                    "wins": 0,
                    "count": 0,
                    "total_duration": 0,
                    "total_pnl": 0,
                    "total_capital": 0,
                    "avg_return_per_dollar": 0,
                    "pnl_list": [],
                    "capital_list": []
                }
            s = summary[strategy]
            s["count"] += 1
            s["total_duration"] += duration
            capital_used = trade.get("capital_used", 0)
            s["total_capital"] += capital_used

            s["total_pnl"] += pnl
            s["pnl_list"].append(pnl)
            if pnl > 0:
                exit_reason = trade.get("exit_reason", "unknown")
                if "exit_reason_stats" not in s:
                    s["exit_reason_stats"] = {}
                if exit_reason not in s["exit_reason_stats"]:
                    s["exit_reason_stats"][exit_reason] = {"count": 0, "pnl": 0}
                s["exit_reason_stats"][exit_reason]["count"] += 1
                s["exit_reason_stats"][exit_reason]["pnl"] += pnl

    # Compute metrics
    for s in summary:
        d = summary[s]
        d["win_rate"] = d["wins"] / d["count"] if d["count"] else 0
        d["avg_duration"] = d["total_duration"] / d["count"]
        d["avg_pnl"] = d["total_pnl"] / d["count"]
        if d["total_capital"] > 0:
            d["avg_return_per_dollar"] = d["total_pnl"] / d["total_capital"]
        else:
            d["avg_return_per_dollar"] = 0

        pnl = d["pnl_list"]
        if len(pnl) >= 2:
            mean = sum(pnl) / len(pnl)
            std = (sum((x - mean)**2 for x in pnl) / (len(pnl) - 1))**0.5
            d["sharpe"] = mean / std if std > 0 else 0
        else:
            d["sharpe"] = 0
        peak = trough = pnl_total = 0
        max_dd = 0
        for p in pnl:
            pnl_total += p
            peak = max(peak, pnl_total)
            dd = peak - pnl_total
            max_dd = max(max_dd, dd)
        d["drawdown"] = max_dd
        #summary[strategy]["pnl_list"].append(pnl)
        #summary[strategy]["capital_list"].append(capital_used)

    return summary

# --------- Optimizers ----------
def optimize_exit_rules(stats, config):
    updated = False
    log_summary = {}

    for strat, perf in stats.items():
        current_rules = config.get(strat, {}).get("exit_signals", [])
        new_rules = current_rules.copy()

        if perf["win_rate"] > 0.5 and perf["sharpe"] > 1.0:
            if "breakout_fail" in new_rules:
                new_rules.remove("breakout_fail")
                updated = True
            if "rsi_drop" in new_rules:
                new_rules.remove("rsi_drop")
                updated = True

        if perf["win_rate"] < 0.3 or perf["drawdown"] > 2.0:
            if "vwap_reversion" not in new_rules:
                new_rules.append("vwap_reversion")
                updated = True

        config.setdefault(strat, {})["exit_signals"] = new_rules
        log_summary[strat] = {
            "win_rate": round(perf["win_rate"], 2),
            "sharpe": round(perf["sharpe"], 2),
            "drawdown": round(perf["drawdown"], 2),
            "new_rules": new_rules,
        }

    return config, updated, log_summary

def optimize_entry_weights(stats, config):
    updated = False
    log_summary = {}
    weights = config.get("strategy_weights", {})

    for strat, perf in stats.items():
        old_weight = weights.get(strat, 1.0)
        if perf["win_rate"] >= 0.55 and perf["sharpe"] > 1.2:
            new_weight = min(old_weight + 0.1, 1.5)
        elif perf["win_rate"] < 0.3 or perf["drawdown"] > 2.5:
            new_weight = max(old_weight - 0.1, 0.5)
        else:
            new_weight = old_weight

        if round(new_weight, 2) != round(old_weight, 2):
            weights[strat] = round(new_weight, 2)
            updated = True

        log_summary[strat] = {
            "old_weight": old_weight,
            "new_weight": weights[strat],
            "win_rate": round(perf["win_rate"], 2),
            "sharpe": round(perf["sharpe"], 2),
            "drawdown": round(perf["drawdown"], 2),
        }

    config["strategy_weights"] = weights
    return config, updated, log_summary

def adjust_strategy_weight(strategy, delta=0.05):
    config = load_config(LIVE_CONFIG_PATH)
    weights = config.get("strategy_weights", {})
    current_weight = weights.get(strategy, 1.0)
    new_weight = max(0.1, round(current_weight + delta, 2))  # Never go below 0.1
    weights[strategy] = new_weight
    config["strategy_weights"] = weights
    save_config(config, LIVE_CONFIG_PATH)
    print(f"🧠 Adjusted weight for {strategy}: {current_weight:.2f} ➝ {new_weight:.2f}")

def evaluate_recovery_from_penalty(penalized, live_params, debug=False):
    tracker = load_json(PENALTY_TRACKER_PATH)
    recovered = []

    for sym in penalized:
        entry = tracker.get(sym, {})
        cooldown_end = entry.get("cooldown_until")
        recent_signals = entry.get("recent_signals", [])

        # Skip if still in cooldown period
        if cooldown_end:
            cooldown_dt = datetime.fromisoformat(cooldown_end)
            if cooldown_dt > datetime.now(TZ):
                continue

        # Evaluate signals
        strong_signals = [s for s in recent_signals if s["confidence"] >= 0.6 and len(s["strategies"]) >= 2]

        # Recover if at least 2 strong recent signals
        if len(strong_signals) >= 2:
            recovered.append(sym)
            if debug:
                print(f"🟢 Recovery conditions met for {sym}, removing from penalty.")

    # Remove recovered symbols
    updated_penalized = [s for s in penalized if s not in recovered]
    live_params["penalized_symbols"] = sorted(updated_penalized)
    atomic_save_json(live_params, LIVE_CONFIG_PATH)

    # Optionally log recovery
    if recovered:
        print(f"✅ Recovered symbols: {recovered}")

#---------- Impact Evaluation ----------
def evaluate_last_decision_impact(current_stats, last_entry):
    """Compare performance before vs. after AI decision to validate impact."""
    try:
        prev_stats = last_entry.get("analysis", {})
        impact_report = {}

        for strategy, curr in current_stats.items():
            prev = prev_stats.get(strategy)
            if not prev:
                continue  # No prior baseline

            delta_win = curr["win_rate"] - prev.get("win_rate", 0)
            delta_pnl = curr["avg_pnl"] - prev.get("avg_pnl", 0)
            delta_duration = curr["avg_duration"] - prev.get("avg_duration", 0)

            impact_report[strategy] = {
                "delta_win_rate": round(delta_win, 3),
                "delta_avg_pnl": round(delta_pnl, 3),
                "delta_avg_duration": round(delta_duration, 1)
            }

        return impact_report
    except Exception as e:
        print(f"⚠️ Error evaluating impact: {e}")
        return {}

# --------- AI Loop ----------

def adjust_weights_based_on_trends(trends, config):
    decisions = {}
    live_weights = config.get("strategy_weights", {})

    for strategy, t in trends.items():
        current_weight = live_weights.get(strategy, 1.0)
        change = 0.0
        reason = "Stable trend"

        if t["win_rate_trend"] > 0.02:
            change = +0.05
            reason = "Improving win rate trend"
        elif t["win_rate_trend"] < -0.02:
            change = -0.05
            reason = "Declining win rate trend"

        new_weight = round(min(max(current_weight + change, 0.5), 2.0), 2)
        if new_weight != current_weight:
            decisions[strategy] = {
                "from": current_weight,
                "to": new_weight,
                "change": change,
                "reason": reason
            }
            live_weights[strategy] = new_weight

    config["strategy_weights"] = live_weights
    return decisions


def optimize_min_hold_times(stats, config):
    updated = False
    log_summary = {}

    for strat, perf in stats.items():
        actual_duration = perf.get("avg_duration", 0) * 60  # minutes -> seconds
        exit_cfg = config.get(strat, {})
        current_min = exit_cfg.get("min_hold_secs", 180)

        if perf["win_rate"] > 0.55 and actual_duration < current_min * 0.8:
            new_min = max(60, int(current_min * 0.9))
            reason = "Good win rate but short duration — reducing min hold"
        elif perf["win_rate"] < 0.3 and actual_duration >= current_min:
            new_min = int(current_min * 1.2)
            reason = "Poor win rate despite full hold — increasing min hold"
        else:
            new_min = current_min
            reason = "No change"

        if new_min != current_min:
            config.setdefault(strat, {})["min_hold_secs"] = new_min
            updated = True

        log_summary[strat] = {
            "current_min": current_min,
            "new_min": new_min,
            "avg_duration": round(actual_duration, 1),
            "reason": reason,
        }

    return config, updated, log_summary

def optimize_trailing_pct(stats, config):
    updated = False
    log_summary = {}
    for strat, perf in stats.items():
        cfg = config.get(strat, {})
        old_pct = cfg.get("trailing_pct", 0.01)
        new_pct = old_pct
        reason = "No change"

        if perf["win_rate"] > 0.6 and perf["avg_duration"] < 4:
            new_pct = min(0.015, old_pct + 0.002)
            reason = "Fast profitable trades – tighten stop"
        elif perf["win_rate"] < 0.3 and perf["drawdown"] > 2:
            new_pct = max(0.005, old_pct - 0.002)
            reason = "Losing fast – loosen stop"

        if round(new_pct, 4) != round(old_pct, 4):
            cfg["trailing_pct"] = round(new_pct, 4)
            config[strat] = cfg
            updated = True

        log_summary[strat] = {
            "old_trailing_pct": old_pct,
            "new_trailing_pct": new_pct,
            "reason": reason,
        }

    return config, updated, log_summary


def optimize_adaptive_hold(stats, config):
    updated = False
    log_summary = {}
    for strat, perf in stats.items():
        cfg = config.get(strat, {})
        old_hold = cfg.get("adaptive_hold_minutes", 180)
        new_hold = old_hold
        reason = "No change"

        if perf["win_rate"] > 0.6 and perf["avg_duration"] < old_hold * 0.7:
            new_hold = max(60, int(old_hold * 0.9))
            reason = "Fast wins – reduce adaptive hold"
        elif perf["win_rate"] < 0.3 and perf["avg_duration"] > old_hold:
            new_hold = int(old_hold * 1.2)
            reason = "Slow losses – extend hold"

        if new_hold != old_hold:
            cfg["adaptive_hold_minutes"] = new_hold
            config[strat] = cfg
            updated = True

        log_summary[strat] = {
            "old_adaptive_hold": old_hold,
            "new_adaptive_hold": new_hold,
            "reason": reason,
        }

    return config, updated, log_summary


def ai_loop():
    while True:
        print("🤖 AI Daemon: Analyzing trade performance...", flush=True)
        trades = load_live_trades()
        if not trades:
            print("⚠️ No trade data found.")
            time.sleep(LOOP_INTERVAL)
            continue

        stats = analyze_trades(trades)
# === 🔍 PENALTY MANAGEMENT BASED ON TRADE & SKIP LOGS ===
        try:
            with open(LIVE_CONFIG_PATH, "r") as f:
                live_params = json.load(f)
        except Exception:
            live_params = {}

        penalized = set(live_params.get("penalized_symbols", []))

        # 1. Analyze skipped_trades.json
        skip_path = "exports/skipped_trades.json"
        if os.path.exists(skip_path):
            with open(skip_path, "r") as f:
                skips = json.load(f)[-200:]
            counts = {}
            for s in skips:
                sym = s.get("symbol")
                if not sym:
                    continue
                counts[sym] = counts.get(sym, 0) + 1

            for sym, ct in counts.items():
                if ct >= 3:
                    penalized.add(sym)

        # 2. Analyze low-PnL trades
        for tlist in trades.values():
            for t in tlist.get("trades", []):
                if t.get("exit_price") and t.get("entry_price"):
                    pnl = (t["exit_price"] - t["entry_price"]) / t["entry_price"]
                    if pnl < -0.02:
                        penalized.add(t["symbol"])

        live_params["penalized_symbols"] = sorted(list(penalized))[-10:]
        evaluate_recovery_from_penalty(penalized, live_params, debug=True)
        atomic_save_json(live_params, LIVE_CONFIG_PATH)
        print("📉 Updated penalized symbols in live_params:", live_params["penalized_symbols"])
        # Load and optimize exit config (min_hold_secs tuning)
        exit_config = load_config(EXIT_CONFIG_PATH)
        exit_config, hold_updated, hold_log = optimize_min_hold_times(stats, exit_config)
        if hold_updated:
                    optimize_exit_rules(stats, exit_config)
        save_config(exit_config, EXIT_CONFIG_PATH)
        
        exit_config, trail_updated, trail_log = optimize_trailing_pct(stats, exit_config)
        exit_config, adapt_updated, adapt_log = optimize_adaptive_hold(stats, exit_config)

        if hold_updated or trail_updated or adapt_updated:
            save_config(exit_config, EXIT_CONFIG_PATH)

        # === DYNAMIC EARLY EXIT TUNING ===
        early_rules = exit_config.get("early_exit_rules", {})
        for strat, perf in stats.items():
            er_stats = perf.get("exit_reason_stats", {})
            scl = er_stats.get("smart_cut_loss", {"count": 0, "pnl": 0})
            epc = er_stats.get("early_profit_capture", {"count": 0, "pnl": 0})

            r = early_rules.get(strat, {})
            if not r:
                # fallback defaults if missing
                r = {
                    "profit_pct": 0.0025,
                    "min_secs": 300,
                    "loss_pct": -0.002,
                    "loss_wait_secs": 600
                }

            # Adjust smart_cut_loss wait time if frequent and losing
            if scl["count"] >= 5 and scl["pnl"] < 0:
                old_wait = r.get("loss_wait_secs", 600)
                new_wait = min(int(old_wait * 1.2), 1800)
                if new_wait != old_wait:
                    r["loss_wait_secs"] = new_wait
                    print(f"🛠️ {strat}: Increased loss_wait_secs {old_wait} ➔ {new_wait}")

            # Adjust early_profit_capture min_secs if frequent and profitable
            if epc["count"] >= 5 and epc["pnl"] > 0:
                old_min = r.get("min_secs", 300)
                new_min = max(int(old_min * 0.9), 120)
                if new_min != old_min:
                    r["min_secs"] = new_min
                    print(f"🛠️ {strat}: Reduced min_secs {old_min} ➔ {new_min}")

            early_rules[strat] = r

        exit_config["early_exit_rules"] = early_rules
        save_config(exit_config, EXIT_CONFIG_PATH)
        print("✅ AI Daemon: Tuned early exit rules based on exit_reason performance.")
        
        # Load AI decision log and get last decision for comparison
        log_data = []
        if os.path.exists(AI_LOG_PATH):
            with open(AI_LOG_PATH, "r") as f:
                try:
                    log_data = json.load(f)
                except json.JSONDecodeError:
                    pass

        last_entry = log_data[-1] if log_data else {}
        impact_report = evaluate_last_decision_impact(stats, last_entry)
        
        # === SIGNAL ENGINE TUNING ===
        signal_config_path = "inputs/ai_strategy_configs/signal_config.json"

        # Load current signal config
        if os.path.exists(signal_config_path):
            with open(signal_config_path, "r") as f:
                signal_config = json.load(f)
        else:
            signal_config = {}

        old_top_n = signal_config.get("top_n_signals", 4)
        old_min_conf = signal_config.get("min_confidence", 0.5)

        # Dynamic adjustment based on average win rate
        avg_win_rate = sum(s.get("win_rate", 0.5) for s in stats.values()) / max(len(stats), 1)

        # Tune number of top signals
        if avg_win_rate > 0.55:
            new_top_n = min(6, old_top_n + 1)
        elif avg_win_rate < 0.3:
            new_top_n = max(2, old_top_n - 1)
        else:
            new_top_n = old_top_n

        # Tune minimum confidence threshold
        new_min_conf = round(min(max(0.4, old_min_conf + (avg_win_rate - 0.5) * 0.2), 0.8), 2)

        # Save if any changes
        if new_top_n != old_top_n or new_min_conf != old_min_conf:
            signal_config["top_n_signals"] = new_top_n
            signal_config["min_confidence"] = new_min_conf
            with open(signal_config_path, "w") as f:
                json.dump(signal_config, f, indent=2)
            print(f"🧠 Updated signal_config.json: top_n_signals={new_top_n}, min_confidence={new_min_conf}")

        
        # Evaluate impact and apply reinforcement or rollback
        reinforcement_log = {}
        for strategy, impact in impact_report.items():
            if impact["delta_win_rate"] < -0.05 or impact["delta_avg_pnl"] < -0.1:
                # Rollback or decrease parameter
                adjust_strategy_weight(strategy, delta=-0.05)
                reinforcement_log[strategy] = "🔁 Rolled back or reduced due to performance drop"
            elif impact["delta_win_rate"] > 0.03 or impact["delta_avg_pnl"] > 0.1:
                # Reinforce by increasing
                adjust_strategy_weight(strategy, delta=0.05)
                reinforcement_log[strategy] = "🚀 Reinforced due to improvement"

        print("📊 Strategy Summary:", flush=True)
        for strategy, data in stats.items():
            print(f"🔹 {strategy}: WinRate={data['win_rate']:.2f}, "
                      f"AvgPnL={data['avg_pnl']:.2f}, Duration={data['avg_duration']:.1f}m", flush=True)

        log_ai_decision({
            "timestamp": datetime.now(pytz.timezone("America/New_York")).isoformat(),
            "analysis": stats,
            "impact_evaluation": impact_report,
            "reinforcement_actions": reinforcement_log,
            "min_hold_tuning": hold_log,
            "trailing_tuning": trail_log,
            "adaptive_hold_tuning": adapt_log,
        })
        print(f"⏱️ Sleeping for {LOOP_INTERVAL} seconds...", flush=True)
        # Determine best_strategy by highest Sharpe ratio
        if stats:
            best_strategy = max(stats.items(), key=lambda x: x[1].get("sharpe", 0))[0]
            avg_sharpe = sum(s.get("sharpe", 0) for s in stats.values()) / max(len(stats), 1)
            avg_dd = sum(s.get("drawdown", 0) for s in stats.values()) / max(len(stats), 1)
        else:
            best_strategy = None
            avg_sharpe = 0
            avg_dd = 0
        # Reward strong performers and penalize weak ones
        for strat, perf in stats.items():
            r = early_rules.get(strat, {})
            if not r:
                continue

            # Reward strong
            if perf["win_rate"] > 0.60 and perf["sharpe"] > 1.5:
                r["profit_pct"] = round(r["profit_pct"] * 1.10, 5)
                r["min_secs"] = int(r["min_secs"] * 1.10)

            # Penalize weak
            elif perf["win_rate"] < 0.40 or perf["drawdown"] > 3:
                r["loss_pct"] = round(r["loss_pct"] * 0.90, 5)
                r["loss_wait_secs"] = int(r["loss_wait_secs"] * 0.85)

            early_rules[strat] = r
        update_system_status("ai_daemon", {
            "top_strategy": best_strategy,
            "sharpe": round(avg_sharpe, 2),
            "drawdown": round(avg_dd, 2)
        })
        time.sleep(LOOP_INTERVAL)
        
if __name__ == "__main__":
    ai_loop()
