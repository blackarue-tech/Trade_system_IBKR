import json
import os
from collections import Counter, defaultdict
from datetime import datetime

EXIT_FAILURES_PATH = "exports/exit_failures.json"
EXIT_ANALYSIS_PATH = "exports/exit_failure_analysis.json"

def run_exit_failure_analysis():
    if not os.path.exists(EXIT_FAILURES_PATH):
        return

    with open(EXIT_FAILURES_PATH) as f:
        failures = json.load(f)

    if not failures:
        return

    by_reason = Counter()
    by_strategy = Counter()
    reason_combos = Counter()
    reasons_by_strategy = defaultdict(list)
    symbols_failed_post_entry = []
    sandbox_failures = {"tested": 0, "failed": 0}

    for f in failures:
        reasons = tuple(sorted(f["exit_reasons"]))
        reason_combos[reasons] += 1
        by_strategy[f["strategy"]] += 1
        for r in f["exit_reasons"]:
            by_reason[r] += 1
            reasons_by_strategy[f["strategy"]].append(r)

        if f.get("failure_type") == "post_entry" or (f.get("pnl", 0) < 0):
            symbols_failed_post_entry.append({
                "symbol": f.get("symbol"),
                "strategy": f.get("strategy", "Unknown"),
                "confidence": f.get("confidence", 0),
                "reason": f.get("exit_reasons", ["unknown"])[0]
            })

        if f.get("entry_sandbox_passed", False):
            sandbox_failures["tested"] += 1
            if f.get("pnl", 0) < 0:
                sandbox_failures["failed"] += 1

    breakdown = {
        "top_reasons": by_reason.most_common(10),
        "top_strategies": by_strategy.most_common(10),
        "common_reason_combinations": reason_combos.most_common(10),
        "reasons_by_strategy": {
            k: Counter(v).most_common(5)
            for k, v in reasons_by_strategy.items()
        },
        "symbols_failed_post_entry": symbols_failed_post_entry,
        "sandbox_failures": sandbox_failures,
        "total_failures": len(failures),
        "last_updated": datetime.now().isoformat()
    }

    with open(EXIT_ANALYSIS_PATH, "w") as f:
        json.dump(breakdown, f, indent=2)

def analyze_exit_failures():
    run_exit_failure_analysis()
    print(f"✅ Exit failure analysis saved to {EXIT_ANALYSIS_PATH}")

if __name__ == "__main__":
    analyze_exit_failures()
