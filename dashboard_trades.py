# dashboard_trades.py - Clean, full new dashboard with Live Open Trades Status tab

import streamlit as st
import pandas as pd
import json
import os
import plotly.express as px
from datetime import datetime
from streamlit_autorefresh import st_autorefresh
import pytz
import warnings
from sklearn.exceptions import DataConversionWarning

from modules.Wallet_Utils import audit_wallet
from modules.questrade import check_api_health
from modules.log_utils import log, fail_safe
from utils import atomic_save_json, update_system_status, load_json

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DataConversionWarning)

TRADE_LOG = "exports/live_trades.json"
WALLET_LOG = "exports/wallet.json"
CONTROL_PATH = "inputs/ai_strategy_configs/control_params.json"
LIVE_PRICES_PATH = "exports/live_prices.json"
LOCAL_TZ = pytz.timezone("America/Toronto")


st.set_page_config(page_title="📈 Live Trade Dashboard", layout="wide")
st.title("📈 Live Trade Monitoring Dashboard test_env4")
live_prices = load_json(LIVE_PRICES_PATH)
is_healthy, msg = check_api_health()
st.markdown(
    f"### 🔌 API Connection Status: <span style='color:{'green' if is_healthy else 'red'}'>{msg}</span>",
    unsafe_allow_html=True
)

auto_refresh = st.checkbox("🔄 Auto-refresh every 20s", value=True)
if auto_refresh:
    st_autorefresh(interval=20000, limit=None, key="live_refresh")

st.markdown("### 🔍 Wallet Integrity Check")
try:
    audit_wallet()
except Exception as e:
    st.error(f"❌ Wallet audit error: {e}")
    log(f"Wallet audit failed: {e}", "ERROR", "Dashboard")

def load_json_safe(path, label="Data"):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        else:
            st.warning(f"{label} file not found: {path}")
            log(f"{label} file not found: {path}", "WARNING", "Dashboard")
    except Exception as e:
        st.error(f"Error loading {label} from {path}: {e}")
        log(f"Error loading {label} from {path}: {e}", "ERROR", "Dashboard")
    return {}

trades_dict = load_json_safe(TRADE_LOG, "Trade log")
wallet = load_json_safe(WALLET_LOG, "Wallet log")
control_params = load_json_safe(CONTROL_PATH, "Control parameters")

try:
    df = pd.DataFrame([
        trade for day in trades_dict.values() for trade in day.get("trades", [])
    ])
except Exception as e:
    st.error(f"❌ Failed to process trade data: {e}")
    log(f"Failed to process trade data: {e}", "ERROR", "Dashboard")
    st.stop()

if df.empty:
    st.warning("No trade data found.")
    st.stop()

try:
    df["datetime"] = pd.to_datetime(df["datetime"], errors='coerce').dt.tz_convert(LOCAL_TZ)
    df["exit_time"] = pd.to_datetime(df["exit_time"], errors='coerce').dt.tz_convert(LOCAL_TZ)
    df["hold_minutes"] = (df["exit_time"] - df["datetime"]).dt.total_seconds() / 60
except Exception as e:
    st.error(f"❌ Error processing datetime fields: {e}")
    log(f"Error processing datetime fields: {e}", "ERROR", "Dashboard")
    st.stop()

tabs = st.tabs(["📊 Today's Summary", "📂 Trade Log", "🔄 Live Open Trades Status"])

# --- Today's Summary Tab ---
with tabs[0]:
    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    df_today = df[
        (df["datetime"].dt.strftime("%Y-%m-%d") == today_str) |
        (df["exit_time"].dt.strftime("%Y-%m-%d") == today_str)
    ]

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

    st.subheader("🛠️ Symbol Control Status")
    col_a, col_b = st.columns(2)
    col_a.write("### 🚫 Suspended Symbols")
    col_a.write(control_params.get("suspended_symbols", []))
    col_b.write("### 🚀 Prioritized Symbols")
    col_b.write(control_params.get("prioritized_symbols", []))

# --- Trade Log Tab ---
with tabs[1]:
    st.subheader("📂 Trade Log")
    st.write("### ✅ Open Trades")
    st.dataframe(
        df[df["status"] == "open"].sort_values("datetime", ascending=False),
        use_container_width=True
    )

    st.write("### 🏁 Closed Trades")
    exit_reason_column = "exit_reasons" if "exit_reasons" in df.columns else "exit_reason"
    exit_reasons = pd.Series([
        reason for sublist in df[exit_reason_column].dropna().tolist()
        if isinstance(sublist, list)
        for reason in sublist
    ]).unique().tolist()
    exit_filter = st.selectbox("Filter by Exit Reason", ["All"] + sorted(exit_reasons))

    df_closed = df[df["status"] == "closed"].copy()
    if exit_filter != "All":
        df_closed = df_closed[
            df_closed[exit_reason_column].apply(
                lambda x: exit_filter in x if isinstance(x, list) else x == exit_filter
            )
        ]

    df_closed = df_closed.sort_values("exit_time", ascending=False)
    st.dataframe(df_closed, use_container_width=True)

# --- Live Open Trades Status Tab ---
with tabs[2]:
    st.subheader("🔄 Live Open Trades Monitor")
    df_open = df[df["status"] == "open"].copy()

    if not df_open.empty:
        now = datetime.now(LOCAL_TZ)
        df_open["time_held_min"] = (now - df_open["datetime"]).dt.total_seconds() / 60
        df_open["current_price"] = df_open["symbol"].apply(
            lambda sym: live_prices.get(sym, df_open.loc[df_open["symbol"] == sym, "entry_price"].values[0])
        )
        df_open["pnl_pct_live"] = (
            (df_open["current_price"] - df_open["entry_price"]) / df_open["entry_price"]
        ) * 100
        df_open["pnl_amount_live"] = (
            (df_open["current_price"] - df_open["entry_price"]) * df_open["shares"]
        )

        df_open["pnl_pct_live"] = df_open["pnl_pct_live"].round(2)
        df_open["pnl_amount_live"] = df_open["pnl_amount_live"].round(2)

        display_cols = [
            "symbol",
            "time_held_min",
            "pnl_pct_live",
            "pnl_amount_live",
            "current_price",
            "shares",
            "entry_price"
        ]
        df_display = df_open[display_cols].sort_values("time_held_min", ascending=False).rename(columns={
            "symbol": "Symbol",
            "time_held_min": "Time Held (min)",
            "pnl_pct_live": "PnL %",
            "pnl_amount_live": "PnL ($)",
            "current_price": "Current Price",
            "shares": "Shares",
            "entry_price": "Entry Price"
        })

        st.dataframe(
            df_display.style.format({
                "Time Held (min)": "{:.1f}",
                "PnL %": "{:.2f}%",
                "PnL ($)": "${:.2f}",
                "Current Price": "${:.2f}",
                "Entry Price": "${:.2f}"
            }),
            use_container_width=True
        )
    else:
        st.info("✅ No open trades currently.")
