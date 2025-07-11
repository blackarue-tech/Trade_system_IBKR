import os
import json
import traceback
from datetime import datetime
from pytz import timezone

LOCAL_TZ = timezone("America/Toronto")

# === CONFIGURE NOTIFICATIONS ===
ENABLE_NOTIFICATIONS = False  # Set to True when you configure Telegram/Discord
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === LOGGING ===

def notify_critical(message):
    if not ENABLE_NOTIFICATIONS:
        return
    try:
        import requests
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                params={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                timeout=10
            )
            log("Sent critical alert via Telegram.", "INFO", "NOTIFY")
    except Exception as e:
        log(f"Failed to send notification: {e}", "ERROR", "NOTIFY")

# === FAIL-SAFE DECORATOR ===
def fail_safe(func=None, *, module="General", critical=False):
    import functools
    def decorator_fail_safe(f):
        @functools.wraps(f)
        def wrapper_fail_safe(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as e:
                log(f"⚠️ Error in {module}: {e}", "ERROR", module)
                if critical:
                    raise
        return wrapper_fail_safe

    if func is None:
        return decorator_fail_safe
    else:
        return decorator_fail_safe(func)

def get_daily_log_path(module_name):
    date_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    log_dir = f"logs/{module_name.lower()}"
    os.makedirs(log_dir, exist_ok=True)
    return f"{log_dir}/{date_str}.log"

def log(message, level="INFO", module="GENERAL"):
    timestamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] [{level}] [{module}] {message}"

    # Print to console
    print(f"[{timestamp}] [{level}] [{module}] {message}")

    # Write to daily file
    log_file = get_daily_log_path(module)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")