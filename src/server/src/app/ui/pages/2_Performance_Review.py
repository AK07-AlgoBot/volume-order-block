"""AK07 Performance Review — charts and strategy summary table."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.services import performance_store
from app.ui.styles import inject_dark_theme

MOCK_MODE = os.environ.get("AK07_MOCK") == "1"

if MOCK_MODE:
    from app.services import mock_data

    mock_data.seed()

st.set_page_config(
    page_title="AK07 Performance Review",
    page_icon="\U0001f4ca",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_dark_theme()

st.markdown("# AK07 Performance Review")

f1, f2, f3 = st.columns([2, 2, 3])
with f1:
    st.caption("Date range")
    range_choice = st.selectbox(
        "Date range",
        ["Today", "Last 7 days", "Last 30 days", "Last 90 days", "All available"],
        index=2,
        label_visibility="collapsed",
    )
with f2:
    st.caption("Mode")
    paper_filter = st.selectbox(
        "Mode",
        ["All", "Paper only", "Live only"],
        index=0,
        label_visibility="collapsed",
    )
with f3:
    if MOCK_MODE:
        st.caption("MOCK DATA — sample trades · sidebar « only for page nav")
    else:
        st.caption("Closed trades · S1 OI · S2 SMC+CRT · S3 BLR Breakout · sidebar « for nav")

today = date.today()
if range_choice == "Today":
    start_date = today
elif range_choice == "Last 7 days":
    start_date = today - timedelta(days=6)
elif range_choice == "Last 30 days":
    start_date = today - timedelta(days=29)
elif range_choice == "Last 90 days":
    start_date = today - timedelta(days=89)
else:
    start_date = today - timedelta(days=365)

trades = performance_store.load_trades(start_date=start_date, end_date=today)

if paper_filter == "Paper only":
    trades = [t for t in trades if t.get("paper_trading")]
elif paper_filter == "Live only":
    trades = [t for t in trades if not t.get("paper_trading")]

summary_rows = performance_store.summarize_by_strategy(trades)
summary_df = pd.DataFrame(summary_rows)
daily_series = performance_store.daily_pnl_series(trades)
daily_df = pd.DataFrame(daily_series) if daily_series else pd.DataFrame()

# --- Section 1: graphs ---
st.markdown("## Performance charts")

if not trades:
    st.info("No completed trades in the selected range yet. Trades appear here when engines record exits.")
else:
    m1, m2, m3, m4 = st.columns(4)
    total_row = summary_rows[-1] if summary_rows else {}
    m1.metric("Total trades", int(total_row.get("Trades", 0)))
    m2.metric("Win rate", f"{total_row.get('Win %', 0)}%")
    m3.metric("Wins / Losses", f"{total_row.get('Wins', 0)} / {total_row.get('Losses', 0)}")
    m4.metric("Total profit (pts)", f"{total_row.get('Profit (pts)', 0):+.2f}")

    g1, g2 = st.columns(2)

    with g1:
        st.markdown("#### Cumulative P&L (points)")
        if not daily_df.empty:
            chart_df = daily_df.set_index("date")[["cumulative_pnl"]]
            st.line_chart(chart_df, height=280)
        else:
            st.caption("No daily series to plot.")

    with g2:
        st.markdown("#### Daily P&L (points)")
        if not daily_df.empty:
            chart_df = daily_df.set_index("date")[["daily_pnl"]]
            st.bar_chart(chart_df, height=280)
        else:
            st.caption("No daily series to plot.")

    g3, g4 = st.columns(2)

    with g3:
        st.markdown("#### Profit by strategy (points)")
        strat_df = summary_df[summary_df["Strategy"] != "TOTAL"].copy()
        if not strat_df.empty and strat_df["Trades"].sum() > 0:
            profit_chart = strat_df.set_index("Strategy")[["Profit (pts)"]]
            st.bar_chart(profit_chart, height=280)
        else:
            st.caption("No strategy breakdown yet.")

    with g4:
        st.markdown("#### Win rate by strategy (%)")
        if not strat_df.empty and strat_df["Trades"].sum() > 0:
            win_chart = strat_df.set_index("Strategy")[["Win %"]]
            st.bar_chart(win_chart, height=280)
        else:
            st.caption("No win-rate breakdown yet.")

    with st.expander("Trade outcome mix", expanded=False):
        if trades:
            outcomes = pd.Series([t.get("result", "BREAKEVEN") for t in trades]).value_counts()
            st.bar_chart(outcomes, height=220)

st.markdown("---")

# --- Section 2: summary table ---
st.markdown("## Strategy summary")

display_df = summary_df.copy()
if not display_df.empty:
    display_df["Win %"] = display_df["Win %"].map(lambda v: f"{v:.1f}%")
    display_df["Profit (pts)"] = display_df["Profit (pts)"].map(lambda v: f"{v:+.2f}")

st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
)

st.caption(
    f"Range: {start_date.isoformat()} → {today.isoformat()} · "
    f"{len(trades)} closed trade(s) · updated {datetime.now().strftime('%H:%M:%S')} local"
)

with st.expander("Raw trade log", expanded=False):
    if trades:
        raw_df = pd.DataFrame(trades)
        cols = [
            c
            for c in (
                "exit_at",
                "strategy",
                "symbol",
                "direction",
                "pnl_points",
                "result",
                "exit_reason",
                "paper_trading",
            )
            if c in raw_df.columns
        ]
        st.dataframe(raw_df[cols], use_container_width=True, hide_index=True)
    else:
        st.caption("No trades to list.")
