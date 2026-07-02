"""AK07 Performance Review — charts and strategy summary table."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.services import performance_store
from app.ui.auth_session import require_login
from app.ui.strategy_access import enabled_strategy_labels_text, user_can_view_trade
from app.ui.styles import inject_dark_theme

MOCK_MODE = os.environ.get("AK07_MOCK") == "1"

if MOCK_MODE:
    from app.services import mock_data

    mock_data.seed()

inject_dark_theme()
require_login()

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
        st.caption(f"Closed trades · {enabled_strategy_labels_text()} · sidebar « for nav")

today = datetime.now(IST).date()
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
trades = [t for t in trades if user_can_view_trade(t)]
load_status = performance_store.load_status(start_date, today)

if paper_filter == "Paper only":
    trades = [t for t in trades if t.get("paper_trading")]
elif paper_filter == "Live only":
    trades = [t for t in trades if not t.get("paper_trading")]

summary_rows = performance_store.summarize_by_strategy(trades)
index_rows = performance_store.summarize_by_index(trades)
matrix_rows = performance_store.summarize_by_strategy_and_index(trades)
summary_df = pd.DataFrame(summary_rows)
index_df = pd.DataFrame(index_rows) if index_rows else pd.DataFrame()
matrix_df = pd.DataFrame(matrix_rows) if matrix_rows else pd.DataFrame()
daily_series = performance_store.daily_pnl_series(trades)
daily_df = pd.DataFrame(daily_series) if daily_series else pd.DataFrame()

# --- Section 1: graphs ---
st.markdown("## Performance charts")

if not trades:
    st.info(
        "No completed trades in the selected range yet. "
        "Trades appear when engines exit positions or when Strategy 1 archives at 15:30 IST."
    )
    with st.expander("Why is this empty?", expanded=True):
        st.markdown(
            f"""
**Data sources checked**

| Source | Status |
|--------|--------|
| Archive folder | `{load_status['archive_dir']}` |
| Folder exists | **{'yes' if load_status['archive_dir_exists'] else 'no'}** |
| Archive files (total) | **{load_status['archive_files_total']}** |
| Archive files in range | **{load_status['archive_files_in_range']}** |
| Legacy archive folder | `{load_status.get('legacy_archive_dir', '—')}` |
| Redis days with trades | **{load_status['redis_days_with_trades']}** |
| Latest archive | `{load_status['latest_archive'] or 'none'}` |

**Notes**
- Strategy 1 writes `performance_review_YYYY-MM-DD.json` at **15:30 IST** (only if the engine ran that day).
- Archives are stored under **`src/server/data/archive`** on the Docker volume — cockpit must mount the same volume as the engine.
- Strategy 2 / 3 record exits to Redis when trades close (after latest deploy).
- If you had old archives inside the container image path, redeploy once — new archives land on the persistent volume.
            """
        )
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

    if not index_df.empty:
        st.markdown("#### Profit by index (points)")
        idx_chart_df = index_df[index_df["Index"] != "TOTAL"].copy()
        if not idx_chart_df.empty and idx_chart_df["Trades"].sum() > 0:
            st.bar_chart(idx_chart_df.set_index("Index")[["Profit (pts)"]], height=240)

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

st.markdown("## Index summary")

if index_df.empty:
    st.caption("No index-level trades in the selected range.")
else:
    display_index_df = index_df.copy()
    display_index_df["Win %"] = display_index_df["Win %"].map(lambda v: f"{v:.1f}%")
    display_index_df["Profit (pts)"] = display_index_df["Profit (pts)"].map(lambda v: f"{v:+.2f}")
    st.dataframe(display_index_df, use_container_width=True, hide_index=True)

st.markdown("## Strategy × index matrix")

if matrix_df.empty:
    st.caption("No strategy/index combinations in the selected range.")
else:
    pivot_profit = matrix_df.pivot_table(
        index="Strategy",
        columns="Index",
        values="Profit (pts)",
        aggfunc="sum",
        fill_value=0.0,
    )
    pivot_trades = matrix_df.pivot_table(
        index="Strategy",
        columns="Index",
        values="Trades",
        aggfunc="sum",
        fill_value=0,
    )
    st.markdown("#### Profit (points)")
    st.dataframe(
        pivot_profit.map(lambda v: f"{v:+.2f}"),
        use_container_width=True,
    )
    st.markdown("#### Trade count")
    st.dataframe(pivot_trades.astype(int), use_container_width=True)

    with st.expander("Detailed strategy × index rows", expanded=False):
        detail_df = matrix_df.copy()
        detail_df["Win %"] = detail_df["Win %"].map(lambda v: f"{v:.1f}%")
        detail_df["Profit (pts)"] = detail_df["Profit (pts)"].map(lambda v: f"{v:+.2f}")
        st.dataframe(detail_df, use_container_width=True, hide_index=True)

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
                "entry_price",
                "exit_price",
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

st.markdown("## Loss review")

if hasattr(performance_store, "analyze_losses"):
    loss_report = performance_store.analyze_losses(trades)
else:
    loss_report = {
        "total_trades": len(trades),
        "wins": sum(1 for t in trades if float(t.get("pnl_points") or 0) > 0.01),
        "losses": sum(1 for t in trades if float(t.get("pnl_points") or 0) < -0.01),
        "loss_rows": [],
        "by_bucket": {},
        "by_strategy": [],
        "filter_note": "Rebuild cockpit image — loss analysis module not deployed yet.",
    }
l1, l2, l3, l4 = st.columns(4)
l1.metric("Total trades", loss_report["total_trades"])
l2.metric("Wins", loss_report["wins"])
l3.metric("Losses", loss_report["losses"])
l4.metric(
    "Win rate",
    f"{(loss_report['wins'] / loss_report['total_trades'] * 100):.1f}%"
    if loss_report["total_trades"]
    else "—",
)

st.caption(loss_report["filter_note"])

if loss_report["losses"]:
    st.markdown("### Why losses happened")
    bucket_df = pd.DataFrame(
        [{"Cause": k, "Count": v} for k, v in sorted(loss_report["by_bucket"].items(), key=lambda x: -x[1])]
    )
    st.dataframe(bucket_df, use_container_width=True, hide_index=True)

    strat_loss_df = pd.DataFrame(loss_report["by_strategy"])
    if not strat_loss_df.empty:
        st.markdown("### Losses by strategy")
        st.dataframe(strat_loss_df, use_container_width=True, hide_index=True)

    loss_df = pd.DataFrame(loss_report["loss_rows"])
    st.markdown("### Every losing trade")
    st.dataframe(loss_df, use_container_width=True, hide_index=True)

    st.markdown(
        """
**Common causes (even with day-review filter on):**
- **Stop-loss hit** — filter picks direction, not outcome; structural S3 SL can be wider than fixed TP.
- **14:55 square-off** — open trade closed at market before TP.
- **S1 / S6 exempt** — can take either side; not gated by S3 day review.
- **Bad levels** — if Green/Red differ from TradingView, entries trigger at wrong prices (see S3 open source: prefer *candle* not *LTP*).
- **Old engine build** — e.g. S1 at 60pt SL instead of 30pt; redeploy `engine` after config changes.
        """
    )
else:
    st.info("No losing trades in the selected range.")
