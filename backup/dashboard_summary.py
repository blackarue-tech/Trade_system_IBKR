import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
import os
import json

st.set_page_config(page_title="AI Trading Summary", layout="wide")

# === Load Data ===
DATA_PATH = "inputs/test_trades_enriched_historical.json"  # JSON file with full history
st.title("📊 AI Trading Summary Dashboard")

@st.cache_data
def load_data():
    with open(DATA_PATH) as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "trades" in raw:
        df = pd.DataFrame(raw["trades"])
    else:
        df = pd.DataFrame(raw)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors='coerce').dt.tz_convert(None)
    if df["datetime"].isna().any():
        st.warning("⚠️ Some 'datetime' values could not be parsed.")
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, errors='coerce').dt.tz_convert(None)
    if df["exit_time"].isna().any():
        st.warning("⚠️ Some 'exit_time' values could not be parsed.")
    df["date"] = df["datetime"].dt.date
    df["win"] = df["pnl"] > 0
    # Ensure strategies_used is a string (convert from list if needed)
    df["strategies_used"] = df["strategies_used"].apply(lambda x: ",".join(x) if isinstance(x, list) else str(x))
    return df

if not os.path.exists(DATA_PATH):
    st.warning("No simulation results found.")
    st.stop()

df = load_data()
df["duration_min"] = (df["exit_time"] - df["datetime"]).dt.total_seconds() / 60

# === Metrics ===
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Trades", len(df))
col2.metric("Win Rate", f"{df['win'].mean() * 100:.2f}%")
col3.metric("Avg PnL", f"{df['pnl'].mean():.2f}")
col4.metric("Avg PnL %", f"{df['pnl_pct'].mean():.2f}%")

# === Sharpe Ratio ===
if df["pnl_pct"].std() != 0:
    sharpe_ratio = df["pnl_pct"].mean() / df["pnl_pct"].std()
else:
    sharpe_ratio = 0
st.subheader("📈 Strategy Performance")
st.write(f"**Sharpe Ratio**: {sharpe_ratio:.2f}")

# === Strategy Usage Bar Chart ===
st.subheader("🧠 Strategy Usage")
strategy_counts = df["strategies_used"].str.split(",").explode().value_counts()
fig, ax = plt.subplots(figsize=(10, 4))
strategy_counts.plot(kind="bar", ax=ax, color="skyblue")
ax.set_ylabel("Count")
ax.set_title("Strategy Usage Frequency")
ax.grid(axis='y', linestyle='--', alpha=0.5)
plt.xticks(rotation=45)
st.pyplot(fig)

# === Filters ===
st.sidebar.header("Filters")
symbols = sorted(df["symbol"].unique())
all_strategies = sorted(set(s.strip() for strat in df["strategies_used"] for s in strat.split(",")))
selected_symbols = st.sidebar.multiselect("Select Symbols", symbols, default=symbols)
selected_strategies = st.sidebar.multiselect("Select Strategies", all_strategies, default=all_strategies)

filtered_df = df[
    df["symbol"].isin(selected_symbols) &
    df["strategies_used"].apply(lambda s: any(x in s for x in selected_strategies))
]

# === Summary Table ===
summary = (
    filtered_df.groupby("symbol")
    .agg(
        trades=("pnl", "count"),
        avg_pnl=("pnl", "mean"),
        win_rate=("win", "mean")
    )
    .sort_values("win_rate", ascending=False)
)
st.subheader("📊 Summary by Symbol")
st.dataframe(summary.style.format({"avg_pnl": "{:.2f}", "win_rate": "{:.1%}"}))

# === Strategy Performance Charts ===
st.subheader("📊 Strategy Metrics")
df_exploded = filtered_df.copy()
df_exploded["strategies_used"] = df_exploded["strategies_used"].str.split(",")
df_exploded = df_exploded.explode("strategies_used")
df_exploded["strategies_used"] = df_exploded["strategies_used"].str.strip()

# --- Average PnL ---
if not df_exploded.empty:
    pnl_avg = df_exploded.groupby("strategies_used")["pnl"].mean().sort_values(ascending=False)
    fig1, ax1 = plt.subplots(figsize=(10, 4))
    bars1 = ax1.bar(pnl_avg.index, pnl_avg.values, color="mediumseagreen")
    ax1.set_ylabel("Avg PnL")
    ax1.set_title("Average PnL per Strategy")
    ax1.grid(axis="y", linestyle="--", alpha=0.5)
    plt.xticks(rotation=45)
    for bar in bars1:
        ax1.annotate(f"{bar.get_height():.2f}", (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                     ha="center", va="bottom", fontsize=8)
    plt.tight_layout()
    st.pyplot(fig1)

    # --- Win Rate ---
    win_avg = df_exploded.groupby("strategies_used")["win"].mean().sort_values(ascending=False)
    fig2, ax2 = plt.subplots(figsize=(10, 4))
    bars2 = ax2.bar(win_avg.index, win_avg.values, color="cornflowerblue")
    ax2.set_ylabel("Win Rate")
    ax2.set_title("Win Rate per Strategy")
    ax2.grid(axis="y", linestyle="--", alpha=0.5)
    plt.xticks(rotation=45)
    for bar in bars2:
        ax2.annotate(f"{bar.get_height():.1%}", (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                     ha="center", va="bottom", fontsize=8)
    plt.tight_layout()
    st.pyplot(fig2)

# === Trades Over Time ===
st.subheader("📆 Trades Over Time")
trades_over_time = (
    filtered_df.groupby("date")
    .agg(avg_pnl=("pnl", "mean"), win_rate=("win", "mean"))
)
st.line_chart(trades_over_time)

# === Trade Log ===
st.subheader("📋 Simulated Trade Log")
st.dataframe(filtered_df.sort_values("datetime", ascending=False), use_container_width=True)
