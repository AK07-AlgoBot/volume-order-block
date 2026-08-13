"""AK07 Performance Review — simple order history for users; analytics for admin."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.services import performance_store
from app.ui.auth_session import is_admin, require_login
from app.ui.strategy_access import (
    enabled_strategy_ids,
    enabled_strategy_labels_text,
    performance_start_floor,
    user_can_view_trade,
)
from app.ui.styles import format_exit_reason_label, inject_dark_theme, summary_chip_row

# Entitlement id → performance_store strategy label used in completed trades.
_PERF_STRATEGY_BY_ENTITLEMENT = {
    "s1_oi": performance_store.STRATEGY_AK07_OI,
    "s2_smc": performance_store.STRATEGY_SMC_CRT,
    "s3_breakout": performance_store.STRATEGY_BREAKOUT,
    "s7_orb": performance_store.STRATEGY_S7_ORB,
    "gamma": performance_store.STRATEGY_GAMMA,
}

MOCK_MODE = os.environ.get("AK07_MOCK") == "1"

if MOCK_MODE:
    from app.services import mock_data

    mock_data.seed()

inject_dark_theme()
require_login()
admin = is_admin()

st.markdown("# Performance")
st.caption("Your closed trades for the selected period — summary chips + order history.")

f1, f2, f3 = st.columns([2, 2, 3])
with f1:
    st.caption("Date range")
    range_choice = st.selectbox(
        "Date range",
        ["Today", "Last 7 days", "Last 30 days", "Last 90 days", "All available"],
        index=0 if not admin else 2,
        label_visibility="collapsed",
        key="perf_range",
    )
with f2:
    st.caption("Mode")
    paper_filter = st.selectbox(
        "Mode",
        ["All", "Paper only", "Live only"],
        index=2 if not MOCK_MODE else 0,
        label_visibility="collapsed",
        key="perf_mode",
    )
with f3:
    if MOCK_MODE:
        st.caption("MOCK DATA — sample trades")
    elif admin:
        st.caption(f"Admin · all users · {enabled_strategy_labels_text()}")
    else:
        st.caption(f"Your trades · from onboarding · {enabled_strategy_labels_text()}")

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

floor = performance_start_floor()
if floor is not None and start_date < floor:
    start_date = floor

trades = performance_store.load_trades(start_date=start_date, end_date=today)
trades = [t for t in trades if user_can_view_trade(t)]
trades = [
    t
    for t in trades
    if str(t.get("strategy") or "") not in performance_store.RETIRED_STRATEGY_LABELS
    and str(t.get("strategy_id") or "") != "s8_choch"
]
load_status = performance_store.load_status(start_date, today)

if paper_filter == "Paper only":
    trades = [t for t in trades if t.get("paper_trading")]
elif paper_filter == "Live only":
    trades = [t for t in trades if not t.get("paper_trading")]

allowed_perf_strategies: list[str] | None = None
if not admin:
    allowed_perf_strategies = [
        _PERF_STRATEGY_BY_ENTITLEMENT[sid]
        for sid in enabled_strategy_ids()
        if sid in _PERF_STRATEGY_BY_ENTITLEMENT
    ]

summary = performance_store.trade_period_summary(trades)
st.markdown(
    summary_chip_row(
        total=summary["trades"],
        wins=summary["wins"],
        losses=summary["losses"],
        pnl_points=summary["pnl_points"],
    ),
    unsafe_allow_html=True,
)
st.caption(
    f"Win rate {summary['win_pct']}% · {start_date.isoformat()} → {today.isoformat()} · "
    f"{len(trades)} closed trade(s)"
)

if not trades:
    st.info(
        "No completed trades in this range yet.\n\n"
        "1. Finish **Token Update** for your broker\n"
        "2. Let a strategy exit\n"
        "3. Refresh this page — chips and the order table fill in"
    )
    if admin:
        with st.expander("Why is this empty?", expanded=False):
            st.markdown(
                f"""
| Source | Status |
|--------|--------|
| Archive folder | `{load_status['archive_dir']}` |
| Folder exists | **{'yes' if load_status['archive_dir_exists'] else 'no'}** |
| Archive files in range | **{load_status['archive_files_in_range']}** |
| Redis days with trades | **{load_status['redis_days_with_trades']}** |
| Latest archive | `{load_status['latest_archive'] or 'none'}` |
                """
            )
else:
    # Optional single daily chart (not both cumulative + daily)
    daily_series = performance_store.daily_pnl_series(trades)
    daily_df = pd.DataFrame(daily_series) if daily_series else pd.DataFrame()
    if not daily_df.empty and len(daily_df) > 1:
        with st.expander("Daily P&L (points)", expanded=False):
            st.bar_chart(daily_df.set_index("date")[["daily_pnl"]], height=220)

# --- Order history (primary surface) ---
st.markdown("### Order history")
st.caption("Closed fills with result and exit reason.")

def _is_s3(t: dict) -> bool:
    return (
        str(t.get("strategy_id") or "") == "breakout"
        or str(t.get("strategy") or "") == performance_store.STRATEGY_BREAKOUT
    )


s3_trades = [t for t in trades if _is_s3(t)]
other_trades = [t for t in trades if not _is_s3(t)]

show_s3 = bool(s3_trades) or (
    allowed_perf_strategies is None
    or performance_store.STRATEGY_BREAKOUT in (allowed_perf_strategies or [])
)

if show_s3:
    st.markdown("#### S3 · BLR Breakout")
    if s3_trades:
        s3_table = performance_store.s3_trade_log_rows(s3_trades)
        for row in s3_table:
            row["Exit reason"] = format_exit_reason_label(str(row.get("Exit reason") or ""))
        st.dataframe(s3_table, use_container_width=True, hide_index=True)
    else:
        st.info("No S3 closes in this range yet.")

if other_trades:
    st.markdown("#### Other strategies")
    other_rows = []
    for t in sorted(other_trades, key=lambda r: str(r.get("exit_at") or ""), reverse=True):
        other_rows.append(
            {
                "Exit at": str(t.get("exit_at") or "")[:19],
                "Strategy": t.get("strategy") or t.get("strategy_id") or "—",
                "Symbol": t.get("symbol") or "—",
                "Direction": t.get("direction") or "",
                "Entry": t.get("entry_price"),
                "Exit": t.get("exit_price"),
                "Actual pts": t.get("pnl_points"),
                "Result": t.get("result")
                or performance_store.classify_result(float(t.get("pnl_points") or 0)),
                "Exit reason": format_exit_reason_label(str(t.get("exit_reason") or "")),
            }
        )
    st.dataframe(other_rows, use_container_width=True, hide_index=True)
elif not show_s3 and not trades:
    pass
elif not show_s3 and trades and not other_trades:
    st.caption("No order rows to display.")

# --- Admin-only analytics (collapsed) ---
if admin and trades:
    st.markdown("---")
    with st.expander("Admin analytics", expanded=False):
        summary_rows = performance_store.summarize_by_strategy(
            trades,
            allowed_strategies=None,
        )
        index_rows = performance_store.summarize_by_index(trades)
        matrix_rows = performance_store.summarize_by_strategy_and_index(trades)
        summary_df = pd.DataFrame(summary_rows)
        index_df = pd.DataFrame(index_rows) if index_rows else pd.DataFrame()
        matrix_df = pd.DataFrame(matrix_rows) if matrix_rows else pd.DataFrame()

        st.markdown("##### Strategy summary")
        display_df = summary_df.copy()
        if not display_df.empty:
            display_df["Win %"] = display_df["Win %"].map(lambda v: f"{v:.1f}%")
            display_df["Profit (pts)"] = display_df["Profit (pts)"].map(lambda v: f"{v:+.2f}")
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        strat_df = summary_df[summary_df["Strategy"] != "TOTAL"].copy() if not summary_df.empty else pd.DataFrame()
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("##### Profit by strategy")
            if not strat_df.empty and strat_df["Trades"].sum() > 0:
                st.bar_chart(strat_df.set_index("Strategy")[["Profit (pts)"]], height=240)
            else:
                st.caption("No strategy breakdown.")
        with c2:
            st.markdown("##### Win rate by strategy")
            if not strat_df.empty and strat_df["Trades"].sum() > 0:
                st.bar_chart(strat_df.set_index("Strategy")[["Win %"]], height=240)
            else:
                st.caption("No win-rate breakdown.")

        st.markdown("##### Index summary")
        if index_df.empty:
            st.caption("No index-level trades.")
        else:
            display_index_df = index_df.copy()
            display_index_df["Win %"] = display_index_df["Win %"].map(lambda v: f"{v:.1f}%")
            display_index_df["Profit (pts)"] = display_index_df["Profit (pts)"].map(
                lambda v: f"{v:+.2f}"
            )
            st.dataframe(display_index_df, use_container_width=True, hide_index=True)

        st.markdown("##### Strategy × index")
        if matrix_df.empty:
            st.caption("No matrix rows.")
        else:
            pivot_profit = matrix_df.pivot_table(
                index="Strategy",
                columns="Index",
                values="Profit (pts)",
                aggfunc="sum",
                fill_value=0.0,
            )
            st.dataframe(
                pivot_profit.map(lambda v: f"{v:+.2f}"),
                use_container_width=True,
            )

        if hasattr(performance_store, "analyze_losses"):
            loss_report = performance_store.analyze_losses(trades)
            st.markdown("##### Loss review")
            st.caption(
                f"Wins {loss_report['wins']} · Losses {loss_report['losses']} · "
                f"{loss_report.get('filter_note') or ''}"
            )
            if loss_report["losses"]:
                bucket_df = pd.DataFrame(
                    [
                        {"Cause": k, "Count": v}
                        for k, v in sorted(
                            loss_report["by_bucket"].items(), key=lambda x: -x[1]
                        )
                    ]
                )
                st.dataframe(bucket_df, use_container_width=True, hide_index=True)
                st.dataframe(
                    pd.DataFrame(loss_report["loss_rows"]),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No losing trades in this range.")
