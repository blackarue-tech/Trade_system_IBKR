#dashboard_trades.py

import streamlit as st
import pandas as pd
import json
import os
import threading
from datetime import datetime, time
from streamlit_autorefresh import st_autorefresh
from dateutil import parser
import pytz
from modules.Wallet_Utils import audit_wallet
from modules.questrade import check_api_health, start_ping_loop

TRADE_LOG = "exports/live_trades.json"
WALLET_LOG = "exports/wallet.json"
LOCAL_TZ = pytz.timezone("America/Toronto")
PING_INTERVAL = 180  # 3 minutes

# Background token keep-alive loop
start_ping_loop(interval_sec=PING_INTERVAL)


st.set_page_config(page_title="📈 Live Trade Dashboard", layout="wide")
st.title("📈 Live Trade Monitoring Dashboard")
# Health status
is_healthy, msg = check_api_health()
st.markdown(f"### 🔌 API Connection Status: <span style='color:{'green' if is_healthy else 'red'}'>{msg}</span>", unsafe_allow_html=True)
# 🔄 Auto-refresh toggle
auto_refresh = st.checkbox("🔄 Auto-refresh every 20s", value=True)
if auto_refresh:
    st_autorefresh(interval=20000, limit=None, key="live_refresh")

# Run wallet audit once per load
st.markdown("### 🔍 Wallet Integrity Check")
audit_wallet(TRADE_LOG, WALLET_LOG)

# === Load Data ===
def load_trades():
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            return json.load(f)
    return {}

def load_wallet():
    if os.path.exists(WALLET_LOG):
        with open(WALLET_LOG) as f:
            return json.load(f)
    return {"capital": 0}

def flatten_trades(trade_dict):
    rows = []
    for date, data in trade_dict.items():
        for trade in data.get("trades", []):
            rows.append(trade)
    return pd.DataFrame(rows)

def convert_and_format_time_local(dt_string):
    if pd.isna(dt_string) or dt_string == "None":
        return None
    dt = parser.isoparse(dt_string)
    local_dt = dt.astimezone(LOCAL_TZ)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")

def fix_trade_times(df):
    for col in ["datetime", "exit_time"]:
        if col in df.columns:
            df[col] = df[col].apply(convert_and_format_time_local)
    return df

# === Prepare Data ===
trades_dict = load_trades()
wallet = load_wallet()
df = flatten_trades(trades_dict)

if df.empty:
    st.warning("No trade data found.")
    st.stop()

df["status"] = df["status"].fillna("open")
df["pnl"] = df.get("pnl", 0.0)

df = fix_trade_times(df)
today_str = datetime.now().strftime("%Y-%m-%d")
df_today = df[df["datetime"].str.startswith(today_str)]

# === Summary Metrics ===
total_trades = len(df_today)
win_rate = (df_today["pnl"] > 0).mean() * 100 if total_trades > 0 else 0
total_pnl = df_today["pnl"].sum()
capital_used = df_today[df_today["status"] == "open"]["capital_used"].sum()
capital_remaining = wallet.get("capital", 0)
capital_total = capital_used + capital_remaining
avg_hold_time = 0
if "exit_time" in df_today.columns and "datetime" in df_today.columns:
    try:
        hold_durations = pd.to_datetime(df_today["exit_time"], errors='coerce') - pd.to_datetime(df_today["datetime"], errors='coerce')
        df_today["hold_time_min"] = hold_durations.dt.total_seconds() / 60
        avg_hold_time = df_today["hold_time_min"].mean()
    except Exception:
        pass

# === Dashboard Display ===
st.subheader("📊 Today's Summary")
col1, col2, col3 = st.columns(3)
col1.metric("Total Trades", total_trades)
col2.metric("Win Rate", f"{win_rate:.2f}%")
col3.metric("Total PnL", f"${total_pnl:.2f}")

col4, col5, col6 = st.columns(3)
col4.metric("Avg Hold Time (min)", f"{avg_hold_time:.2f}")
col5.metric("Capital Deployed", f"${capital_used:.2f}")
col6.metric("Remaining Capital", f"${capital_remaining:.2f}")

st.metric("💼 Total Capital (Deployed + Wallet)", f"${capital_total:.2f}")

# === Open/Closed Trade Tables ===
st.subheader("📂 Trade Log")
open_trades = df[df["status"] == "open"]
closed_trades = df[df["status"] == "closed"].copy()

# Convert exit_time to datetime object for sorting
closed_trades["_exit_dt"] = closed_trades["exit_time"].apply(lambda x: parser.isoparse(x).astimezone(LOCAL_TZ) if pd.notna(x) else pd.NaT)

st.write("### ✅ Open Trades")
st.dataframe(open_trades.sort_values("datetime", ascending=False), use_container_width=True)

st.write("### 🏁 Closed Trades")

# Filter by exit reason
exit_reasons = closed_trades["exit_reason"].dropna().unique().tolist()
exit_filter = st.selectbox("Filter by Exit Reason", ["All"] + sorted(exit_reasons))

filtered_closed = closed_trades.copy()
if exit_filter != "All":
    filtered_closed = filtered_closed[filtered_closed["exit_reason"] == exit_filter]

filtered_closed = filtered_closed.sort_values("_exit_dt", ascending=False)
st.dataframe(filtered_closed.drop(columns="_exit_dt"), use_container_width=True)
