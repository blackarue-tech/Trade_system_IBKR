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


from modules.questrade import check_api_health
from modules.log_utils import log, fail_safe
from utils import atomic_save_json, update_system_status, load_json

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DataConversionWarning)

TRADE_LOG = "exports/live_trades.json"

CONTROL_PATH = "inputs/ai_strategy_configs/control_params.json"
LIVE_PRICES_PATH = "exports/live_prices.json"
LOCAL_TZ = pytz.timezone("America/Toronto")


st.set_page_config(page_title="📈 Live Trade Dashboard", layout="wide")
st.title("📈 Live Trade Monitoring Dashboard test_env6 _ IBKR integration")
live_prices = load_json(LIVE_PRICES_PATH)


auto_refresh = st.checkbox("🔄 Auto-refresh every 20s", value=True)
if auto_refresh:
    st_autorefresh(interval=20000, limit=None, key="live_refresh")

st.markdown("### 🔍 Wallet Integrity Check")


def _col_or_none(df: pd.DataFrame, col: str) -> pd.Series:
    # Return the column if present, else a None-filled Series aligned to df.index
    return df[col] if col in df.columns else pd.Series([None]*len(df), index=df.index)

def _from_dict(series: pd.Series, key: str):
    # Safely pull a key from dict-like cells
    return series.map(lambda d: d.get(key) if isinstance(d, dict) else None)

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
    
# --- Pending age (minutes) for pending_* rows only ---
def _parse_any_ts(val):
    try:
        # Parse anything into a tz-aware timestamp (UTC), then convert to LOCAL_TZ
        ts = pd.to_datetime(val, utc=True, errors="coerce")
        return ts.tz_convert(LOCAL_TZ)
    except Exception:
        return pd.NaT

def _infer_pending_since_row(row: pd.Series):
    if row.get("status") not in ("pending_buy", "pending_sell"):
        return pd.NaT
    pe = row.get("pending_escalation")
    if isinstance(pe, dict) and isinstance(pe.get("history"), list) and pe["history"]:
        tss = [_parse_any_ts(h.get("ts")) for h in pe["history"] if isinstance(h, dict)]
        tss = [t for t in tss if pd.notna(t)]
        if tss:
            return min(tss)
    # Fallback to the trade's start time
    return _parse_any_ts(row.get("datetime"))

# Build the column
df["pending_since"] = df.apply(_infer_pending_since_row, axis=1)

# **Force** the whole column to be tz-aware (UTC) then convert to LOCAL_TZ
# (This avoids mixed tz-aware/naive objects that trigger the subtraction error)
ps = pd.to_datetime(df["pending_since"], utc=True, errors="coerce")
df["pending_since"] = ps.dt.tz_convert(LOCAL_TZ)

# Compute minutes only for pending rows
now_ts = pd.Timestamp.now(tz=LOCAL_TZ)
diff_min = (now_ts - df["pending_since"]).dt.total_seconds() / 60
df["pending_minutes"] = diff_min.where(df["status"].isin(["pending_buy", "pending_sell"]))

# Normalize exit_time to tz-aware and compute "pending minutes at close"
df["exit_time"] = pd.to_datetime(df.get("exit_time"), utc=True, errors="coerce").dt.tz_convert(LOCAL_TZ)

# How long the order spent pending before it closed
df["pending_minutes_at_close"] = (
    (df["exit_time"] - df["pending_since"]).dt.total_seconds() / 60
)
# Only meaningful for closed rows
df.loc[df["status"] != "closed", "pending_minutes_at_close"] = pd.NA
# ---- Normalize nested broker fields for dashboard convenience ----
# Flatten orderId and limit price
df["buy_orderId"] = df.get("broker_order").apply(lambda d: d.get("orderId") if isinstance(d, dict) else None)
df["sell_orderId"] = df.get("broker_order_sell").apply(lambda d: d.get("orderId") if isinstance(d, dict) else None)
df["buy_limit"] = df.get("broker_order").apply(lambda d: d.get("price") if isinstance(d, dict) else None)
df["sell_limit"] = df.get("broker_order_sell").apply(lambda d: d.get("price") if isinstance(d, dict) else None)

# Unified columns the UI can use
df["orderId"] = df.apply(
    lambda r: r["buy_orderId"] if r.get("status") == "pending_buy"
    else (r["sell_orderId"] if r.get("status") == "pending_sell" else None),
    axis=1
)
df["limit"] = df.apply(
    lambda r: r["buy_limit"] if r.get("status") == "pending_buy"
    else (r["sell_limit"] if r.get("status") == "pending_sell" else None),
    axis=1
)
try:
    df["datetime"] = pd.to_datetime(df["datetime"], errors='coerce').dt.tz_convert(LOCAL_TZ)
    df["exit_time"] = pd.to_datetime(df["exit_time"], errors='coerce').dt.tz_convert(LOCAL_TZ)
    df["hold_minutes"] = (df["exit_time"] - df["datetime"]).dt.total_seconds() / 60
except Exception as e:
    st.error(f"❌ Error processing datetime fields: {e}")
    log(f"Error processing datetime fields: {e}", "ERROR", "Dashboard")
    st.stop()

tabs = st.tabs(["📊 Today's Summary", "📂 Trade Log", "🟢 IBKR Open Trades", "⏳ Pending Orders"])

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

    # --- NEW: capital allocation metrics ---
    # Positions still tying up capital = open or pending_sell
    df_positions = df[df["status"].isin(["open", "pending_sell"])].copy()
    pos_price = pd.to_numeric(
        df_positions["entry_price"].fillna(df_positions["intended_entry_price"]),
        errors="coerce"
    ).fillna(0)
    pos_shares = pd.to_numeric(df_positions["shares"], errors="coerce").fillna(0)
    allocated_usd = float((pos_price * pos_shares).sum())

    # Orders not yet filled = pending_buy (use intended_entry_price as best estimate)
    df_pbuy = df[df["status"] == "pending_buy"].copy()
    pbuy_price = pd.to_numeric(
        df_pbuy["intended_entry_price"].fillna(df_pbuy["entry_price"]),
        errors="coerce"
    ).fillna(0)
    pbuy_shares = pd.to_numeric(df_pbuy["shares"], errors="coerce").fillna(0)
    pending_usd = float((pbuy_price * pbuy_shares).sum())
    roof = 10_000.0  # must match CAPITAL_LIMIT in engine
    total_exposure = allocated_usd + pending_usd
    available_cap = max(0.0, roof - total_exposure)
    
    st.subheader("📊 Today's Summary")
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Trades", int(total_trades))
    col2.metric("Win Rate", f"{win_rate:.2f}%")
    col3.metric("Total PnL", f"${total_pnl:,.2f}")
    col4.metric("Allocated (Open + Pending Sell)", f"${allocated_usd:,.2f}")
    col5.metric("Pending Buys Notional", f"${pending_usd:,.2f}")
    st.caption(f"💡 Total Exposure = ${total_exposure:,.2f}  •  Roof = ${roof:,.2f}  •  Available = ${available_cap:,.2f}")

    

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

    # NEW: add rounded pending minutes at close to the same table
    if "pending_minutes_at_close" in df_closed.columns:
        df_closed["pending_minutes"] = pd.to_numeric(
            df_closed["pending_minutes_at_close"], errors="coerce"
        ).round(1)

    st.dataframe(df_closed, use_container_width=True)
    # --- fill pending minutes in Closed Trades (fallbacks: pending_since -> exit_armed_at -> datetime)
    et = pd.to_datetime(df_closed.get("exit_time"), utc=True, errors="coerce")
    ps = pd.to_datetime(df_closed.get("pending_since"), utc=True, errors="coerce")
    ea = pd.to_datetime(df_closed.get("exit_armed_at"), utc=True, errors="coerce")
    dt = pd.to_datetime(df_closed.get("datetime"), utc=True, errors="coerce")

    # fill missing pending_since with fallbacks
    ps_filled = ps.where(ps.notna(), ea).where(ps.notna(), dt)

    # minutes spent pending up to close
    df_closed["pending_minutes"] = ((et - ps_filled).dt.total_seconds() / 60).round(1)

# --- New: IBKR Open Trades Monitor (executed open positions only) ---
with tabs[2]:
    st.subheader("🟢 IBKR Open Trades (Executed)")
    df_open = df[df["status"] == "open"].copy()
    if df_open.empty:
        st.info("✅ No executed open trades.")
    else:
        now = datetime.now(LOCAL_TZ)
        df_open["time_held_min"] = (now - df_open["datetime"]).dt.total_seconds() / 60
        df_open["current_price"] = df_open["symbol"].apply(
            lambda s: live_prices.get(s, df_open.loc[df_open["symbol"] == s, "entry_price"].values[0])
        )
        base_entry = df_open["entry_price"].fillna(df_open["intended_entry_price"])
        df_open["pnl_pct_live"] = ((df_open["current_price"] - base_entry) / base_entry) * 100
        df_open["pnl_amount_live"] = (df_open["current_price"] - base_entry) * df_open["shares"]

        display_cols = ["symbol","time_held_min","pnl_amount_live","pnl_pct_live","current_price","shares","entry_price","intended_entry_price"]
        view = df_open[display_cols].sort_values("time_held_min", ascending=False).rename(columns={
            "symbol":"Symbol","time_held_min":"Time Held (min)","pnl_amount_live":"PnL ($)","pnl_pct_live":"PnL (%)",
            "current_price":"Current Price","shares":"Shares","entry_price":"Entry Price (Exec/Target)","intended_entry_price":"Entry Price (Intended)"
        })
        st.dataframe(view.style.format({"Time Held (min)":"{:.1f}","PnL ($)":"${:.2f}","PnL (%)":"{:.2f}%","Current Price":"${:.2f}","Entry Price (Target)":"${:.2f}","Entry Price (Exec)":"${:.2f}"}), use_container_width=True)

# --- New: Pending Orders Monitor (buy & sell) ---
with tabs[3]:
    st.subheader("⏳ Pending Orders (Open/Close)")
    df_pending = df[df["status"].isin(["pending_buy", "pending_sell"])].copy()
    if df_pending.empty:
        st.info("✅ No pending orders.")
    else:
        for col in ["entry_price", "exit_price", "shares", "datetime", "orderId", "limit"]:
            if col not in df_pending.columns:
                df_pending[col] = None

        # show age in minutes (rounded to 1 decimal)
        df_pending["pending_minutes"] = pd.to_numeric(df_pending["pending_minutes"], errors="coerce").round(1)

        cols = ["symbol","status","orderId","shares","entry_price","exit_price","limit","pending_minutes"]
        renamed = {
            "symbol":"Symbol","status":"Status","orderId":"Order ID","shares":"Shares",
            "entry_price":"Entry Px","exit_price":"Exit Px","limit":"Limit Px",
            "pending_minutes":"Pending (min)"
        }
        view = df_pending[cols].rename(columns=renamed).sort_values("Pending (min)", ascending=False)
        st.dataframe(view, use_container_width=True)


def _trade_status_badge(s):
    m = {
        "open": "🟢 open",
        "closed": "⚪ closed",
        "pending_buy": "🟡 pending buy",
        "pending_sell": "🟠 pending sell",
        "error_buy": "🔴 buy error",
        "error_sell": "🔴 sell error",
    }
    return m.get(s, s)