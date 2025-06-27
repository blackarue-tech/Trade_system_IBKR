
import json
import os
import time
import shutil
from datetime import datetime

def atomic_save_json(data, path, retries=3, delay=1):
    tmp_path = path + ".tmp"
    bak_path = path + ".bak"

    try:
        # Write to tmp
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)

        # Backup current file
        if os.path.exists(path):
            shutil.copy2(path, bak_path)

        # Retry renaming
        for attempt in range(retries):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as e:
                print(f"⚠️ Attempt {attempt+1}: Failed to replace {path} — {e}")
                time.sleep(delay)

        # All attempts failed, restore backup
        print(f"❌ atomic_save_json failed: restoring backup from {bak_path}")
        if os.path.exists(bak_path):
            shutil.copy2(bak_path, path)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        print("⚠️ Save atomic.")
    except Exception as e:
        print(f"❌ atomic_save_json error: {e}")

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
    except Exception as e:
        print(f"⚠️ Failed to update system status for {engine_name}: {e}")