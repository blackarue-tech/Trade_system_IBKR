#questrade.py
import os
import json
import requests
from datetime import datetime, timedelta
import logging
logging.basicConfig(
    level=logging.INFO,  # Change to logging.DEBUG for more verbosity
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("token_debug.log")  # You could add FileHandler() here if needed
    ]
)

TOKEN_PATH = os.path.expanduser("C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json")
TOKEN_EXPIRY_MARGIN = timedelta(minutes=5)
REFRESH_URL = "https://login.questrade.com/oauth2/token"

def save_token(data):
    logging.debug("Saving token to file...")
    with open(TOKEN_PATH, "w") as f:
        json.dump(data, f)
    logging.info("Token saved successfully.")

def get_token_data():
    if not os.path.exists(TOKEN_PATH):
        raise FileNotFoundError("Token file not found. Please authorize manually.")
    with open(TOKEN_PATH) as f:
        return json.load(f)

def token_expired(token_data):
    expiry = datetime.strptime(token_data["expires_at"], "%Y-%m-%dT%H:%M:%S")
    return datetime.utcnow() + TOKEN_EXPIRY_MARGIN > expiry

def refresh_token(token_data):
    print("🔄 Refreshing Questrade token...")
    response = requests.get(
        f"https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token={token_data['refresh_token']}"
    )
    if response.status_code != 200:
        raise RuntimeError("Failed to refresh token")

    new_token = response.json()
    new_token["refresh_token"] = token_data["refresh_token"]  # preserve original
    new_token["expires_at"] = (datetime.utcnow() + timedelta(seconds=new_token["expires_in"])).strftime("%Y-%m-%dT%H:%M:%S")

    with open(TOKEN_PATH, "w") as f:
        json.dump(new_token, f)

    return new_token
def load_token():
    logging.debug("Loading token from file...")
    #print(f"🔍 Looking for token at: {TOKEN_PATH}")
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r") as f:
            data = json.load(f)
        logging.info(f"Token loaded. Expiry: {data.get('expiry')}")
        return data
    else:
        print("❌ Token file not found at the expected path.")
        logging.warning("Token file does not exist.")
    return None

def get_access_token(token_path="questrade_token.json"):
    #print("🔄 I am here looking for token...")
    if not os.path.exists(token_path):
        raise FileNotFoundError("Token file not found.")

    with open(token_path, "r") as f:
        token_data = json.load(f)

    # Check expiry and refresh if needed
    expiry_time = datetime.fromisoformat(token_data["expiry"])
    if expiry_time <= datetime.now():
        print("🔄 Token expired, refreshing...")
        response = requests.get(
            f"https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token={token_data['refresh_token']}"
        )
        if response.status_code != 200:
            raise Exception("Failed to refresh token.")

        new_data = response.json()
        new_data["expiry"] = (datetime.now() + timedelta(seconds=new_data["expires_in"])).isoformat()
        new_data["api_server"] = new_data["api_server"].rstrip("/")
        with open(token_path, "w") as f:
            json.dump(new_data, f, indent=2)

        print("✅ Refreshed and saved new token.")
        return new_data

    print("✅ Token access valid.")
    return token_data

def get_symbol_id(symbol, token_path="C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json"):
    """Fetch the Questrade symbol ID for a given stock ticker."""
    token_data = get_access_token(token_path)
    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    url = f"{token_data['api_server']}/v1/symbols/search?prefix={symbol}"
    #print("🔄 I am here looking for Symbol ID...")
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch symbol ID for {symbol}")

    data = response.json()
    matches = [item for item in data.get("symbols", []) if item["symbol"].upper() == symbol.upper()]
    if not matches:
        raise ValueError(f"Symbol {symbol} not found in Questrade symbol search")
    #print(f"🔄 Found symbol ID for {symbol}: {matches[0]['symbolId']}")
    return matches[0]["symbolId"]

def get_quote(symbol_id, token_path="C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json"):
    """Fetch the latest quote for a given symbol ID."""
    token_data = get_access_token(token_path)
    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    url = f"{token_data['api_server']}/v1/markets/quotes/{symbol_id}"
    print("🔄 I am here looking for quote...")
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch quote for symbolId {symbol_id}")

    data = response.json()
    if "quotes" in data and data["quotes"]:
        print("🔄 I am here looking for quote and responding...")
        return data["quotes"][0]  # return the full quote dictionary
    else:
        raise ValueError(f"No quote data returned for symbolId {symbol_id}")