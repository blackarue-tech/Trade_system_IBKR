# brokers/ibkr_state.py
import os, json, tempfile, shutil
from datetime import date, datetime  # add this

STATE_PATH = os.path.join("exports", "ibkr_state.json")

def _json_default(o):
    # Safely serialize datetimes (and anything else) to strings
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"orders_open": [], "positions": [], "fills": []}  # optional: updated keys
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(d):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        json.dump(d, tmp, indent=2, default=_json_default)  # <-- here
        tmp_path = tmp.name
    shutil.move(tmp_path, STATE_PATH)
