# utils.py - Structured logging + fail-safe atomic utilities

import json
import os
import time
import shutil
from datetime import datetime
from filelock import FileLock, Timeout
from modules.log_utils import log

def atomic_save_json(data, path, retries=5, delay=1):
    #lock_path = path + ".lock"
    tmp_path = path + ".tmp"

    if isinstance(data, str) and not isinstance(path, str):
        data, path = path, data
    lock_path = path + ".lock"
    
    for attempt in range(1, retries + 1):
        try:
            with FileLock(lock_path, timeout=10):
                # Cleanup stale tmp if exists
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                        log(f"Removed stale tmp file {tmp_path}", "DEBUG", "Utils")
                    except Exception as e:
                        log(f"Failed to remove stale tmp file {tmp_path}: {e}", "WARNING", "Utils")

                # Write data to tmp safely
                with open(tmp_path, "w") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())

                # Replace atomically
                os.replace(tmp_path, path)
                log(f"Atomic save successful: {path}", "DEBUG", "Utils")

                # Short wait to stabilize under Windows
                time.sleep(0.05)
                return

        except Timeout:
            log(f"[Attempt {attempt}] Timeout acquiring lock for {path}, retrying...", "WARNING", "Utils")
        except PermissionError as e:
            log(f"[Attempt {attempt}] PermissionError on {path}: {e}. Retrying...", "WARNING", "Utils")
        except FileNotFoundError as e:
            log(f"[Attempt {attempt}] FileNotFoundError during atomic save of {path}: {e}. Retrying...", "WARNING", "Utils")
        except Exception as e:
            log(f"[Attempt {attempt}] atomic_save_json failed for {path}: {e}", "ERROR", "Utils")

        time.sleep(delay)

    log(f"❌ atomic_save_json ultimately failed for {path} after {retries} retries.", "ERROR", "Utils")
    
def update_system_status(engine_name, info, path="exports/system_status.json"):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                status = json.load(f)
        else:
            status = {}

        status[engine_name] = {
            "timestamp": datetime.now().isoformat(),
            **info
        }

        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(status, f, indent=2)
        os.replace(tmp_path, path)

        #log(f"System status updated for {engine_name}.", "INFO", "Utils")

    except Exception as e:
        log(f"Failed to update system status for {engine_name}: {e}", "ERROR", "Utils")

def load_json(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        log(f"Error loading JSON from {path}: {e}", "ERROR", "IO")
    return {}

def save_json(data, path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Error saving JSON to {path}: {e}", "ERROR", "IO")
