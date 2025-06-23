#exit_monitor.py – Enhanced Exit Dashboard with Summary Stats

import streamlit as st
import pandas as pd
import json
import os
from datetime import datetime, timezone
from dateutil import parser
import pytz
from modules.questrade import get_quote

TRADES_FILE = "exports/live_trades.json"
WALLET_FILE = "exports/wallet.json"
LOCAL_TZ = pytz.timezone("America/Toronto")

st.set_page_config("🚨 Exit Monitor", layout="wide")
st.title("🚨 Exit Executor Monitor")

def load_live_trades():
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE) as f:
        data = json.load(f)
        all_trades = []
        for day in data:
            for t in data[day].get("trades", []):
                t["date"] = day
                all_trades.append(t)
        return all_trades

def get_pending_trades(trades):
    return [t for t in trades if t.get("exit_price") is None]

def get_closed_trades(trades):
    return [t for t in trades if t.get("exit_price") is not None]

def get_live_pnl(entry_price, current_price, quantity):
    pnl = (current_price - entry_price) * quantity
    pnl_pct = (current_price - entry_price) / entry_price * 100
    return pnl, pnl_pct

def load_wallet():
    if os.path.exists(WALLET_FILE):
        with open(WALLET_FILE) as f:
            return json.load(f)
    return {"capital": 0.0}

def format_datetime_local(iso_str):
    try:
        dt = parser.isoparse(iso_str).astimezone(LOCAL_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return iso_str

def build_summary_stats(closed_trades):
    df = pd.DataFrame(closed_trades)
    df["pnl"] = df["pnl"].astype(float)
    df["win"] = df["pnl"] > 0

    # By exit reason
    by_reason = df.groupby("exit_reason").agg(
        total_trades=("pnl", "count"),
        total_pnl=("pnl", "sum"),
        win_rate=("win", "mean")
    ).reset_index()
    by_reason["win_rate"] = (by_reason["win_rate"] * 100).round(2)

    # By strategy (flattened)
    rows = []
    for t in closed_trades:
        for s in t.get("strategies_used", []):
            rows.append({"strategy": s, "pnl": t.get("pnl", 0), "win": t.get("pnl", 0) > 0})
    strat_df = pd.DataFrame(rows)
    by_strategy = strat_df.groupby("strategy").agg(
        total_trades=("pnl", "count"),
        total_pnl=("pnl", "sum"),
        win_rate=("win", "mean")
    ).reset_index()
    by_strategy["win_rate"] = (by_strategy["win_rate"] * 100).round(2)

    return by_reason, by_strategy

def display_exit_monitor():
    trades = load_live_trades()
    wallet = load_wallet()
    pending_trades = get_pending_trades(trades)
    closed_trades = get_closed_trades(trades)

    st.sidebar.metric("💰 Wallet Capital", f"${wallet.get('capital', 0.0):,.2f}")
    st.sidebar.caption(f"Last updated: {wallet.get('last_updated', 'N/A')}")

    st.subheader("🔍 Filter Settings")
    strategy_filter = st.multiselect("Filter by Strategy", options=sorted({s for t in trades for s in t.get("strategies_used", [])}))
    reason_filter = st.multiselect("Filter by Exit Reason", options=sorted({t.get("exit_reason") for t in trades if t.get("exit_reason")}))
    symbol_filter = st.multiselect("Symbols", options=sorted({t["symbol"] for t in trades}))
    show_closed = st.checkbox("Show Closed Trades", value=True)

    st.markdown("---")
    st.subheader("📡 Pending Exit Trades")
    if not pending_trades:
        st.success("✅ No trades pending exit.")
    else:
        table = []
        for t in pending_trades:
            try:
                quote = get_quote(t["symbolId"])
                current_price = quote.get("lastTradePrice") or quote.get("mark") or 0.0
                pnl, pnl_pct = get_live_pnl(t["entry_price"], current_price, t["shares"])
                duration = (datetime.now(timezone.utc) - parser.isoparse(t["datetime"]).astimezone(timezone.utc)).total_seconds() / 60
                table.append({
                    "Symbol": t["symbol"],
                    "Entry Time": format_datetime_local(t["datetime"]),
                    "Entry": t["entry_price"],
                    "Current": round(current_price, 2),
                    "Qty": t["shares"],
                    "Duration (min)": round(duration, 1),
                    "PnL": round(pnl, 2),
                    "PnL %": round(pnl_pct, 2),
                    "Strategy": ", ".join(t.get("strategies_used", [])),
                    "Confidence": t.get("confidence"),
                })
            except Exception as e:
                table.append({
                    "Symbol": t["symbol"],
                    "Entry Time": format_datetime_local(t["datetime"]),
                    "Entry": t["entry_price"],
                    "Current": "⚠️ Error",
                    "Qty": t["shares"],
                    "Duration (min)": "—",
                    "PnL": "—",
                    "PnL %": "—",
                    "Strategy": ", ".join(t.get("strategies_used", [])),
                    "Confidence": t.get("confidence"),
                })

        df_table = pd.DataFrame(table)
        if strategy_filter:
            df_table = df_table[df_table["Strategy"].apply(lambda s: any(f in s for f in strategy_filter))]
        if symbol_filter:
            df_table = df_table[df_table["Symbol"].isin(symbol_filter)]
        st.dataframe(df_table, use_container_width=True)

    if show_closed:
        st.markdown("---")
        st.subheader("🏁 Recently Closed Trades")
        closed = []
        for t in closed_trades:
            closed.append({
                "Symbol": t["symbol"],
                "Entry Time": format_datetime_local(t["datetime"]),
                "Exit Time": format_datetime_local(t.get("exit_time")),
                "Entry": t["entry_price"],
                "Exit": t["exit_price"],
                "Qty": t["shares"],
                "PnL": round(t.get("pnl", 0), 2),
                "PnL %": round(t.get("pnl_pct", 0), 2),
                "Strategy": ", ".join(t.get("strategies_used", [])),
                "Reason": t.get("exit_reason"),
                "Confidence": t.get("confidence")
            })

        df_closed = pd.DataFrame(closed)
        if reason_filter:
            df_closed = df_closed[df_closed["Reason"].isin(reason_filter)]
        if strategy_filter:
            df_closed = df_closed[df_closed["Strategy"].apply(lambda s: any(f in s for f in strategy_filter))]
        if symbol_filter:
            df_closed = df_closed[df_closed["Symbol"].isin(symbol_filter)]

        df_closed = df_closed.sort_values("Exit Time", ascending=False)
        st.dataframe(df_closed, use_container_width=True)

        # 📊 Summary Stats
        st.markdown("---")
        st.subheader("📊 Exit Performance Summary")
        by_reason, by_strategy = build_summary_stats(closed_trades)

        st.write("**By Exit Reason**")
        st.dataframe(by_reason, use_container_width=True)

        st.write("**By Strategy Used**")
        st.dataframe(by_strategy, use_container_width=True)

if __name__ == "__main__":
    display_exit_monitor()
