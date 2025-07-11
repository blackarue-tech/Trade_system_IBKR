# price_data.py - Polygon.io + Questrade fail-safe, structured logging

import os
import requests
import json
from datetime import datetime, timedelta, timezone
import time
import pandas as pd

from pytz import timezone as pytz_timezone
from modules.questrade import get_access_token
from modules.log_utils import log, fail_safe
from dotenv import load_dotenv

load_dotenv()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
LOCAL_TZ = pytz_timezone("America/Toronto")

if not POLYGON_API_KEY:
    log("⚠️ POLYGON_API_KEY not set. Check your .env file.", "WARNING", "PolygonUtils")


@fail_safe(module="PriceData")
def get_price_data(symbol, days=20, interval="minute"):
    #log(f"Fetching Polygon price data for {symbol}...", "INFO", "PriceData")
    if POLYGON_API_KEY is None:
        log("POLYGON_API_KEY is not set.", "ERROR", "PriceData")
        return pd.DataFrame()

    try:
        end_date = datetime.now(LOCAL_TZ)
        start_date = end_date - pd.Timedelta(days=days)
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/"
            f"{interval}/{start_date.date()}/{end_date.date()}?adjusted=true&sort=asc&apiKey={POLYGON_API_KEY}"
        )

        response = requests.get(url, timeout=10)
        data = response.json()

        if "results" not in data:
            log(f"No data returned for {symbol}. Response: {data}", "WARNING", "PriceData")
            return pd.DataFrame()

        df = pd.DataFrame(data["results"])
        if df.empty:
            log(f"Empty results for {symbol}.", "WARNING", "PriceData")
            return pd.DataFrame()

        df["datetime"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(LOCAL_TZ)
        df.rename(columns={
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume"
        }, inplace=True)

        df = df[["datetime", "open", "high", "low", "close", "volume"]].dropna()
        df.sort_values("datetime", inplace=True)
        df.reset_index(drop=True, inplace=True)

        #log(f"Retrieved {len(df)} rows for {symbol}.", "INFO", "PriceData")
        return df

    except Exception as e:
        log(f"Error fetching Polygon data for {symbol}: {e}", "ERROR", "PriceData")
        return pd.DataFrame()

@fail_safe(module="PriceData")
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
                response = requests.get(url, headers=headers, params=params, timeout=10)
                response.raise_for_status()
                candles = response.json().get("candles", [])
                if not candles:
                    log(f"No candle data for {symbol_id}.", "WARNING", "PriceData")
                    return pd.DataFrame()
                df = pd.DataFrame(candles)
                df["datetime"] = pd.to_datetime(df["start"], utc=True).dt.tz_convert(LOCAL_TZ)
                result_df = df[["datetime", "open", "high", "low", "close", "volume"]].dropna().sort_values("datetime").reset_index(drop=True)
                #log(f"Retrieved {len(result_df)} candles for {symbol_id}.", "INFO", "PriceData")
                return result_df
            except Exception as e:
                log(f"Chart fetch failed for {symbol_id} (attempt {attempt + 1}): {e}", "WARNING", "PriceData")
                time.sleep(2 ** attempt)

    except Exception as e:
        log(f"Token error during chart fetch for {symbol_id}: {e}", "ERROR", "PriceData")

    return pd.DataFrame()
