#questrade.py
import requests
import json
import time
import os
import csv
from datetime import datetime, timedelta, timezone
import pytz
import string
import logging
import threading
import pandas as pd
import streamlit as st

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("questrade_api.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

TOKEN_PATH = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json")

# ✅ Ping API using safe_questrade_request
def ping_api():
    data = safe_questrade_request("/v1/accounts")
    if data:
        #print("✅ Ping successful - API connection alive.")
        st.session_state.last_ping = time.time()
        return True
    else:
        print("❌ Ping failed.")
        return False

def start_ping_loop(interval_sec=300):
    def ping_loop():
        while True:
            ping_api()
            time.sleep(interval_sec)
    threading.Thread(target=ping_loop, daemon=True).start()

def check_api_health():
    try:
        token_data = get_access_token()
        access_token = token_data["access_token"]
        api_server = token_data["api_server"]

        api_server = api_server.rstrip("/")
        url = f"{api_server}/v1/accounts"
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            return True, "✅ API and token are valid"
        else:
            return False, f"❌ Token rejected: {response.status_code}"
    except Exception as e:
        return False, f"❌ Exception: {e}"

# Load or refresh access token
def get_access_token(token_path=TOKEN_PATH):
    if not os.path.exists(token_path):
        raise FileNotFoundError("Token file not found.")

    with open(token_path) as f:
        data = json.load(f)

    access_token = data.get("access_token")
    api_server = data.get("api_server")
    expires_at = data.get("expires_at")
    refresh_token = data.get("refresh_token")

    if not access_token or not api_server:
        raise ValueError("Invalid token structure.")

    # Check expiration
    if expires_at:
        expires = datetime.fromisoformat(expires_at)
        if datetime.now(timezone.utc) >= expires:
            print("🔁 Access token expired. Refreshing...")
            new_data = refresh_token_request(refresh_token)
            with open(token_path, "w") as f:
                json.dump(new_data, f, indent=2)
            access_token = new_data["access_token"]
            api_server = new_data["api_server"]

    #logging.info("✅ Access token successfully loaded.")

    return {
        "access_token": access_token,
        "api_server": api_server,
        "refresh_token": refresh_token
    }

def refresh_token_request(refresh_token):
    url = f"https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token={refresh_token}"
    r = requests.get(url)
    if r.status_code != 200:
        raise ValueError("Failed to refresh token")
    new_data = r.json()
    new_data["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=new_data["expires_in"])).isoformat()
    return new_data

# Unified safe request
def safe_questrade_request(endpoint, token_path=TOKEN_PATH, params=None, max_retries=2):
    attempt = 0
    while attempt < max_retries:
        try:
            token_data = get_access_token(token_path)
            headers = {"Authorization": f"Bearer {token_data['access_token']}"}
            url = f"{token_data['api_server']}{endpoint}"

            #logging.info(f"🔎 Request attempt {attempt + 1}: {url}")
            response = requests.get(url, headers=headers, params=params)

            if response.status_code == 401:
                logging.warning("⚠️ Token rejected. Attempting auto-refresh...")
                token_data = get_access_token(token_path)  # triggers refresh
                headers = {"Authorization": f"Bearer {token_data['access_token']}"}
                url = f"{token_data['api_server']}{endpoint}"
                response = requests.get(url, headers=headers, params=params)

            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed for {endpoint}: {e}")
            attempt += 1
            time.sleep(1)

    logging.error(f"❌ All {max_retries} attempts failed for endpoint: {endpoint}")
    return None

# Quote fetch using safe wrapper
def get_quote(symbol_id, token_path=TOKEN_PATH):
    return get_quote_data(symbol_id, token_path)

def get_quote_data(symbol_id, token_path=TOKEN_PATH):
    data = safe_questrade_request(f"/v1/markets/quotes/{symbol_id}", token_path=token_path)
    if data and "quotes" in data and data["quotes"]:
        return data["quotes"][0]
    raise ValueError(f"No quote data returned for symbolId {symbol_id}")

# Add other existing utility functions below if needed...
def get_symbol_id(symbol, token_path=TOKEN_PATH):
    data = safe_questrade_request(f"/v1/symbols/search?prefix={symbol}", token_path=token_path)
    if data and "symbols" in data and data["symbols"]:
        return data["symbols"][0]["symbolId"]
    raise ValueError(f"No symbolId found for {symbol}")