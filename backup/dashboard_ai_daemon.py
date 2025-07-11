#dashboard_ai_daemon.py
import streamlit as st
import pandas as pd
import json
import os
from datetime import datetime
import matplotlib.pyplot as plt
import seaborn as sns

st.set_page_config(page_title="AI Daemon Strategy Dashboard", layout="wide")
st.title("🤖 AI Daemon Monitoring Dashboard")

# Load historical strategy logs
log_dir = "logs/strategy_stats"
files = sorted([f for f in os.listdir(log_dir) if f.endswith(".json")])
if not files:
    st.warning("No strategy logs found in logs/strategy_stats")
    st.stop()

records = []
for fname in files:
    with open(os.path.join(log_dir, fname)) as f:
        data = json.load(f)
        for strat, stats in data.items():
            records.append({
                "date": fname.replace(".json", ""),
                "strategy": strat,
                **stats
            })
df = pd.DataFrame(records)
df["date"] = pd.to_datetime(df["date"])

# Sidebar filters
strategies = df["strategy"].unique().tolist()
selected = st.sidebar.multiselect("Filter Strategies", strategies, default=strategies)

filtered = df[df["strategy"].isin(selected)].copy()
filtered.sort_values(["strategy", "date"], inplace=True)

tab1, tab2, tab3 = st.tabs(["📈 Win Rate & PnL", "🛠 Exit Thresholds", "📊 Full Stats Table"])

with tab1:
    st.subheader("Win Rate & PnL Over Time")
    fig, ax1 = plt.subplots(figsize=(12, 5))
    for strat in selected:
        sub = filtered[filtered["strategy"] == strat]
        ax1.plot(sub["date"], sub["win_rate"], label=f"{strat} Win Rate")
    ax1.set_ylabel("Win Rate")
    ax1.set_xlabel("Date")
    ax1.legend()
    ax1.grid(True)
    st.pyplot(fig)

    fig, ax2 = plt.subplots(figsize=(12, 5))
    for strat in selected:
        sub = filtered[filtered["strategy"] == strat]
        ax2.plot(sub["date"], sub["avg_pnl"], label=f"{strat} Avg PnL")
    ax2.set_ylabel("Avg PnL (%)")
    ax2.set_xlabel("Date")
    ax2.legend()
    ax2.grid(True)
    st.pyplot(fig)

with tab2:
    st.subheader("Exit Threshold Evolution")
    exit_path = "inputs/ai_strategy_configs/exit_params.json"
    if os.path.exists(exit_path):
        with open(exit_path) as f:
            exits = json.load(f).get("early_exit_rules", {})
        expander = st.expander("Current Exit Rules")
        expander.json(exits)

    fig, ax3 = plt.subplots(figsize=(12, 5))
    for strat in selected:
        sub = filtered[filtered["strategy"] == strat]
        ax3.plot(sub["date"], sub["avg_duration"], label=f"{strat} Avg Duration")
    ax3.set_ylabel("Avg Duration (min)")
    ax3.set_xlabel("Date")
    ax3.legend()
    ax3.grid(True)
    st.pyplot(fig)

with tab3:
    st.subheader("Full Daily Strategy Metrics")
    st.dataframe(filtered.reset_index(drop=True), use_container_width=True)
    
tab4 = st.tabs(["🚀 Live Trades"])[0]

with tab4:
    st.subheader("Active & Recent Trades")
    live_path = "exports/live_trades.json"
    if os.path.exists(live_path):
        with open(live_path) as f:
            data = json.load(f)
            all_trades = []
            for d in data.values():
                all_trades.extend(d["trades"])
        df_trades = pd.DataFrame(all_trades)
        df_trades["datetime"] = pd.to_datetime(df_trades["datetime"])
        df_trades.sort_values("datetime", ascending=False, inplace=True)
        st.dataframe(df_trades.head(30), use_container_width=True)
    else:
        st.warning("No live_trades.json found.")

    st.subheader("Wallet Capital & Trade Queue")
    wallet_path = "exports/wallet.json"
    queue_path = "exports/trade_queue.json"
    if os.path.exists(wallet_path):
        with open(wallet_path) as f:
            wallet = json.load(f)
        st.json(wallet)

    if os.path.exists(queue_path):
        with open(queue_path) as f:
            queue = json.load(f)
        df_queue = pd.DataFrame(queue)
        st.write("Queued Signals")
        st.dataframe(df_queue, use_container_width=True)
