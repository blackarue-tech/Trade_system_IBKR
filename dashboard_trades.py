# dashboard_trades.py - Enhanced with Symbol Control, Trading Duration, and PnL Graphs

import streamlit as st
import pandas as pd
import json
import os
import plotly.express as px
from datetime import datetime
from streamlit_autorefresh import st_autorefresh
import pytz
from modules.Wallet_Utils import audit_wallet
from modules.questrade import check_api_health, start_ping_loop

TRADE_LOG = "exports/live_trades.json"
WALLET_LOG = "exports/wallet.json"
CONTROL_PATH = "inputs/ai_strategy_configs/control_params.json"
LOCAL_TZ = pytz.timezone("America/Toronto")
PING_INTERVAL = 180

start_ping_loop(interval_sec=PING_INTERVAL)

st.set_page_config(page_title="📈 Live Trade Dashboard", layout="wide")
st.title("📈 Live Trade Monitoring Dashboard")

is_healthy, msg = check_api_health()
st.markdown(f"### 🔌 API Connection Status: <span style='color:{'green' if is_healthy else 'red'}'>{msg}</span>", unsafe_allow_html=True)

auto_refresh = st.checkbox("🔄 Auto-refresh every 20s", value=True)
if auto_refresh:
    st_autorefresh(interval=20000, limit=None, key="live_refresh")

st.markdown("### 🔍 Wallet Integrity Check")
audit_wallet()

# Load Data
def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

trades_dict = load_json(TRADE_LOG)
wallet = load_json(WALLET_LOG)
control_params = load_json(CONTROL_PATH)

df = pd.DataFrame([trade for day in trades_dict.values() for trade in day.get("trades", [])])

if df.empty:
    st.warning("No trade data found.")
    st.stop()

df["datetime"] = pd.to_datetime(df["datetime"], errors='coerce').dt.tz_convert(LOCAL_TZ)
df["exit_time"] = pd.to_datetime(df["exit_time"], errors='coerce').dt.tz_convert(LOCAL_TZ)
df["hold_minutes"] = (df["exit_time"] - df["datetime"]).dt.total_seconds() / 60

today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
df_today = df[df["datetime"].dt.strftime("%Y-%m-%d") == today_str]

# Summary Metrics
total_trades = len(df_today)
win_rate = (df_today["pnl"] > 0).mean() * 100 if total_trades > 0 else 0
total_pnl = df_today["pnl"].sum()
capital_used = df_today[df_today["status"] == "open"]["capital_used"].sum()
capital_remaining = wallet.get("balance", 0)
capital_total = capital_used + capital_remaining
avg_hold_time = df_today["hold_minutes"].mean() if "hold_minutes" in df_today else 0

st.subheader("📊 Today's Summary")
col1, col2, col3 = st.columns(3)
col1.metric("Total Trades", total_trades)
col2.metric("Win Rate", f"{win_rate:.2f}%")
col3.metric("Total PnL", f"${total_pnl:.2f}")

col4, col5, col6 = st.columns(3)
col4.metric("Capital Deployed", f"${capital_used:.2f}")
col5.metric("Remaining Capital", f"${capital_remaining:.2f}")
col6.metric("Avg Hold Time (min)", f"{avg_hold_time:.2f}")

st.metric("💼 Total Capital (Deployed + Wallet)", f"${capital_total:.2f}")

# Symbol Control View
st.subheader("🛠️ Symbol Control Status")
col_a, col_b = st.columns(2)
col_a.write("### 🚫 Suspended Symbols")
col_a.write(control_params.get("suspended_symbols", []))
col_b.write("### 🚀 Prioritized Symbols")
col_b.write(control_params.get("prioritized_symbols", []))

# Trade Log Tables
st.subheader("📂 Trade Log")
st.write("### ✅ Open Trades")
st.dataframe(df[df["status"] == "open"].sort_values("datetime", ascending=False), use_container_width=True)

st.write("### 🏁 Closed Trades")
exit_reason_column = "exit_reasons" if "exit_reasons" in df.columns else "exit_reason"
exit_reasons = pd.Series([reason for sublist in df[exit_reason_column].dropna().tolist() if isinstance(sublist, list) for reason in sublist]).unique().tolist()
exit_filter = st.selectbox("Filter by Exit Reason", ["All"] + sorted(exit_reasons))

df_closed = df[df["status"] == "closed"].copy()
if exit_filter != "All":
    df_closed = df_closed[df_closed[exit_reason_column].apply(lambda x: exit_filter in x if isinstance(x, list) else x == exit_filter)]

df_closed = df_closed.sort_values("exit_time", ascending=False)
st.dataframe(df_closed, use_container_width=True)

# PnL Performance Graphs
st.subheader("📈 PnL Performance by Symbol")
if "pnl" in df.columns and "symbol" in df.columns:
    pnl_summary = df.groupby("symbol")["pnl"].sum().reset_index().sort_values("pnl", ascending=False)
    fig = px.bar(pnl_summary, x="symbol", y="pnl", title="Total PnL per Symbol", text_auto=True)
    st.plotly_chart(fig, use_container_width=True)
