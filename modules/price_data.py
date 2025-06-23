# price_data.py

import os
import requests
import json
from datetime import datetime, timedelta, timezone
import time
import pandas as pd
from pytz import timezone as pytz_timezone
from modules.questrade import get_access_token

LOCAL_TZ = pytz_timezone("America/Toronto")

def get_price_data(symbol_id, days=20, interval="OneDay", token_path="C:/Users/ricar/OneDrive/Documents/Python/questrade_token.json"):
    print(f"🔍 Fetching price data for {symbol_id}...")
    try:
        token_data = get_access_token(token_path=token_path)
        if not isinstance(token_data, dict):
            print("❌ Token access error: token_data is not a dictionary.")
            return None

        token = token_data["access_token"]
        api_server = token_data["api_server"]

        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days)

        url = f"{api_server}/v1/markets/candles/{symbol_id}"
        params = {
            "startTime": start_time.isoformat() + "Z",
            "endTime": end_time.isoformat() + "Z",
            "interval": interval
        }
        headers = {"Authorization": f"Bearer {token}"}

        response = requests.get(url, headers=headers, params=params)
        if response.status_code != 200:
            print(f"❌ Token API error: {response.status_code} {response.text}")
            return None

        candles = response.json().get("candles", [])
        if not candles:
            print("⚠️ No candle data returned.")
            return None

        df = pd.DataFrame(candles)
        if "start" not in df:
            print("⚠️ 'start' column missing in data.")
            return None

        df["datetime"] = pd.to_datetime(df["start"], utc=True).dt.tz_convert(LOCAL_TZ)
        df = df[["datetime", "open", "high", "low", "close", "volume"]].dropna()
        df.sort_values("datetime", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    except Exception as e:
        print(f"❌ General error fetching data for {symbol_id}: {e}")
        return None


def get_chart_price_data(symbol_id, days=5, interval="OneDay", max_retries=3):
    try:
        token_data = get_access_token()
        access_token = token_data["access_token"]
        api_server = token_data["api_server"]

        headers = {"Authorization": f"Bearer {access_token}"}
        end_time = datetime.now(timezone.utc).replace(microsecond=0)
        start_time = end_time - timedelta(days=days)

        params = {
            "startTime": start_time.astimezone().isoformat(),
            "endTime": end_time.astimezone().isoformat(),
            "interval": interval
        }

        url = f"{api_server}/v1/markets/candles/{symbol_id}"

        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, params=params)
                response.raise_for_status()
                candles = response.json().get("candles", [])
                if not candles:
                    print(f"⚠️ No candle data for {symbol_id}")
                    return pd.DataFrame()
                df = pd.DataFrame(candles)
                df["datetime"] = pd.to_datetime(df["start"], utc=True).dt.tz_convert(LOCAL_TZ)
                return df[["datetime", "open", "high", "low", "close", "volume"]].dropna().sort_values("datetime").reset_index(drop=True)
            except Exception as e:
                print(f"⚠️ Chart fetch failed (attempt {attempt+1}): {e}")
                time.sleep(2 ** attempt)

    except Exception as e:
        print(f"❌ Token error in chart fetch: {e}")

    return pd.DataFrame()
