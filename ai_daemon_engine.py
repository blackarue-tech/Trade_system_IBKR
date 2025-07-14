# ai_daemon_engine.py

import os
import json
import schedule
import time
from datetime import datetime
from modules.log_utils import log
from ai_daemon import adaptive_tune

AI_DAEMON_STATUS = "exports/ai_daemon_status.json"

def save_json(data, path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Error saving JSON to {path}: {e}", "ERROR", "IO")

def job_run_ai_daemon():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"⚡ Running adaptive_tune() at {now}", "INFO", "AIDaemonEngine")
    try:
        adaptive_tune()
        save_json({"last_run": now}, AI_DAEMON_STATUS)
    except Exception as e:
        log(f"❌ Error in adaptive_tune: {e}", "ERROR", "AIDaemonEngine")

def main():
    log("✅ AI Daemon Engine Started.", "INFO", "AIDaemonEngine")
    log(" - Adaptive tuning scheduled every 60 minutes.", "INFO", "AIDaemonEngine")

    schedule.every(60).minutes.do(job_run_ai_daemon)

    # Initial run at startup
    job_run_ai_daemon()

    while True:
        schedule.run_pending()
        time.sleep(5)

if __name__ == "__main__":
    main()
