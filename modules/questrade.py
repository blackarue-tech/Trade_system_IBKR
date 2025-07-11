# questrade.py - Now with structured logging and fail-safe handling

import requests
import json
import time
import os
import csv
from datetime import datetime, timedelta, timezone
import pytz
import string
import threading
import pandas as pd
import streamlit as st
import warnings
from sklearn.exceptions import DataConversionWarning
from modules.log_utils import log, fail_safe

TOKEN_PATH = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json")
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DataConversionWarning)
# === PING & HEALTH ===

@fail_safe(module="Questrade")
def ping_api():
    data = safe_questrade_request("/v1/accounts")
    if data:
        st.session_state.last_ping = time.time()
        #log("Ping successful - API connection alive.", "INFO", "Questrade")
        return True
    else:
        log("Ping failed.", "ERROR", "Questrade")
        return False

def start_ping_loop(interval_sec=300):
    def ping_loop():
        while True:
            ping_api()
            time.sleep(interval_sec)
    threading.Thread(target=ping_loop, daemon=True).start()

@fail_safe(module="Questrade")
def check_api_health():
    try:
        token_data = get_access_token()
        access_token = token_data["access_token"]
        api_server = token_data["api_server"]

        api_server = api_server.rstrip("/")
        url = f"{api_server}/v1/accounts"
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            return True, "✅ API and token are valid"
        else:
            return False, f"❌ Token rejected: {response.status_code}"
    except Exception as e:
        return False, f"❌ Exception: {e}"

# === TOKEN MANAGEMENT ===

def get_access_token(token_path=TOKEN_PATH):
    if not os.path.exists(token_path):
        log("Token file not found.", "ERROR", "Questrade")
        raise FileNotFoundError("Token file not found.")

    with open(token_path) as f:
        data = json.load(f)

    access_token = data.get("access_token")
    api_server = data.get("api_server")
    expires_at = data.get("expires_at")
    refresh_token = data.get("refresh_token")

    if not access_token or not api_server:
        log("Invalid token structure.", "ERROR", "Questrade")
        raise ValueError("Invalid token structure.")

    if expires_at:
        expires = datetime.fromisoformat(expires_at)
        if datetime.now(timezone.utc) >= expires:
            log("Access token expired. Refreshing...", "WARNING", "Questrade")
            new_data = refresh_token_request(refresh_token)
            with open(token_path, "w") as f:
                json.dump(new_data, f, indent=2)
            access_token = new_data["access_token"]
            api_server = new_data["api_server"]

    #log("Access token loaded successfully.", "INFO", "Questrade")

    return {
        "access_token": access_token,
        "api_server": api_server,
        "refresh_token": refresh_token
    }

def refresh_token_request(refresh_token):
    url = f"https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token={refresh_token}"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        log(f"Failed to refresh token: {r.status_code}", "ERROR", "Questrade")
        raise ValueError("Failed to refresh token")
    new_data = r.json()
    new_data["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=new_data["expires_in"])).isoformat()
    log("Token refreshed successfully.", "INFO", "Questrade")
    return new_data

# === SAFE REQUEST WRAPPER ===

def safe_questrade_request(endpoint, token_path=TOKEN_PATH, params=None, max_retries=3):
    attempt = 0
    force_refresh = False

    while attempt < max_retries:
        try:
            token_data = get_access_token(token_path)

            if force_refresh:
                log("Forcing token refresh due to prior failure...", "WARNING", "Questrade")
                token_data = refresh_token_request(token_data['refresh_token'])
                with open(token_path, "w") as f:
                    json.dump(token_data, f, indent=2)

            headers = {"Authorization": f"Bearer {token_data['access_token']}"}
            url = f"{token_data['api_server'].rstrip('/')}{endpoint}"
            response = requests.get(url, headers=headers, params=params, timeout=10)

            if response.status_code == 401:
                log(f"Token rejected (401) on attempt {attempt + 1}. Refreshing and retrying.", "WARNING", "Questrade")
                force_refresh = True
                attempt += 1
                continue

            if response.status_code == 504:
                log(f"504 timeout on attempt {attempt + 1}. Retrying...", "WARNING", "Questrade")
                time.sleep(1.5)
                attempt += 1
                continue

            response.raise_for_status()
            return response.json()

        except Exception as e:
            log(f"Attempt {attempt + 1} failed for {endpoint}: {e}", "ERROR", "Questrade")
            attempt += 1
            time.sleep(1.5)

    log(f"All {max_retries} attempts failed for endpoint: {endpoint}", "ERROR", "Questrade")
    return None

# === PUBLIC API FUNCTIONS ===

@fail_safe(module="Questrade")
def get_quote(symbol_id, token_path=TOKEN_PATH):
    return get_quote_data(symbol_id, token_path)

@fail_safe(module="Questrade")
def get_quote_data(symbol_id, token_path=TOKEN_PATH):
    data = safe_questrade_request(f"/v1/markets/quotes/{symbol_id}", token_path=token_path)
    if data and "quotes" in data and data["quotes"]:
        #log(f"Quote retrieved for symbolId {symbol_id}.", "INFO", "Questrade")
        return data["quotes"][0]
    else:
        log(f"No quote data returned for symbolId {symbol_id}.", "WARNING", "Questrade")
        return {}

@fail_safe(module="Questrade")
def get_symbol_id(symbol, token_path=TOKEN_PATH):
    data = safe_questrade_request(f"/v1/symbols/search?prefix={symbol}", token_path=token_path)
    if data and "symbols" in data and data["symbols"]:
        symbol_id = data["symbols"][0]["symbolId"]
        #log(f"Symbol ID for {symbol}: {symbol_id}", "INFO", "Questrade")
        return symbol_id
    else:
        log(f"No symbolId found for {symbol}.", "WARNING", "Questrade")
        return None
