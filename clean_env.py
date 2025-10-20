# scripts/clean_env.py
import os, shutil, time, json, tempfile

TS = time.strftime("%Y%m%d_%H%M%S")

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def rotate_file(path):
    if os.path.exists(path):
        ensure_dir("backups")
        base = os.path.basename(path)
        bak = os.path.join("backups", f"{base}.bak.{TS}")
        shutil.copy2(path, bak)
        # truncate original if it's a log/state file
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]\n" if path.endswith("_log.json") else "{}\n")

def main():
    # 1) remove daemon lock
    ensure_dir("runtime")
    lock = os.path.join("runtime", "ibkr_status_daemon.lock")
    if os.path.exists(lock):
        os.remove(lock)

    # 2) rotate IBKR state/logs
    ensure_dir("exports")
    for p in [
        "exports/ibkr_state.json",
        "exports/ibkr_orders_log.json",
        "exports/system_status.json",
    ]:
        rotate_file(p)

    # 3) back up live_trades.json (never delete)
    lt = "exports/live_trades.json"
    if os.path.exists(lt):
        ensure_dir("backups")
        shutil.copy2(lt, f"backups/live_trades.json.bak.{TS}")

    # 4) optional volatile caches
    # for p in ["exports/live_prices.json","exports/last_exit_tracker.json","exports/exit_failures.json"]:
    #     if os.path.exists(p): os.remove(p)

    print(f"✅ Clean done @ {TS}")

if __name__ == "__main__":
    main()
