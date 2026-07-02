"""Stock OI Scanner — searchable NSE / BSE / MCX instrument picker + OI matrix."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.config.paths import ensure_repo_and_lib_on_path
from app.services.instrument_catalog import (
    QUICK_PICKS,
    InstrumentPick,
    catalog_status,
    ensure_catalog,
    search_instruments,
    search_instruments_api,
)
from app.services.stock_oi_analyzer import analyze_and_matrix, load_credentials_for_user
from app.ui.auth_session import current_username, require_login
from app.ui.styles import inject_dark_theme

ensure_repo_and_lib_on_path()

inject_dark_theme()
require_login()

USERNAME = current_username()

st.markdown("# Stock OI Scanner")
st.caption(
    "Search any NSE, BSE, or MCX instrument (type at least 3 letters), pick from matches, "
    "then run the OI trade matrix."
)

token, base_url = load_credentials_for_user(USERNAME)
if token:
    st.caption(f"Upstox token loaded for **{USERNAME}** · base `{base_url}`")
else:
    st.warning("No Upstox access token on file. Open **Token Update** in the sidebar and paste a fresh token.")

if "oi_selected_pick" not in st.session_state:
    st.session_state.oi_selected_pick = QUICK_PICKS[0]
if "oi_search_query" not in st.session_state:
    st.session_state.oi_search_query = "NIFTY"

status_col, sync_col = st.columns([4, 1])
with status_col:
    meta = catalog_status()
    if meta["ready"]:
        st.caption(
            f"Instrument catalog: **{meta['count']:,}** symbols "
            f"(NSE/BSE equities & indices, MCX futures) · updated {str(meta.get('updated_at') or '')[:19]}"
        )
    else:
        st.caption("Instrument catalog not synced yet — first search will download the Upstox master file.")
with sync_col:
    if st.button("Sync catalog", use_container_width=True):
        with st.spinner("Syncing Upstox instrument master…"):
            ok, msg = ensure_catalog()
        if ok:
            st.success(msg)
        else:
            st.error(msg)
        st.rerun()

st.markdown("**Quick picks**")
qp_cols = st.columns(len(QUICK_PICKS))
for col, pick in zip(qp_cols, QUICK_PICKS):
    with col:
        if st.button(pick.trading_symbol, use_container_width=True):
            st.session_state.oi_selected_pick = pick
            st.session_state.oi_search_query = pick.trading_symbol
            st.rerun()

search_query = st.text_input(
    "Search symbol (min 3 letters)",
    value=st.session_state.oi_search_query,
    placeholder="e.g. REL, TCS, CRU, BAN, SENSEX",
    help="Matches trading symbol and name across NSE, BSE, and MCX futures.",
).strip()

if search_query != st.session_state.oi_search_query:
    st.session_state.oi_search_query = search_query

matches: list[InstrumentPick] = []
if len(search_query) >= 3:
    if not catalog_status()["ready"]:
        with st.spinner("Loading instrument catalog (one-time daily sync)…"):
            ensure_catalog()
    matches = search_instruments(search_query)
    if not matches and token:
        matches = search_instruments_api(search_query, token, base_url)
elif search_query:
    st.info("Type at least **3 letters** to search the full NSE / BSE / MCX catalog.")

selected_pick: InstrumentPick | None = st.session_state.oi_selected_pick

if matches:
    labels = [m.label for m in matches]
    default_index = 0
    if selected_pick:
        for idx, match in enumerate(matches):
            if match.instrument_key == selected_pick.instrument_key:
                default_index = idx
                break
    chosen_label = st.selectbox(
        f"Select instrument ({len(matches)} match{'es' if len(matches) != 1 else ''})",
        labels,
        index=default_index,
    )
    selected_pick = matches[labels.index(chosen_label)]
    st.session_state.oi_selected_pick = selected_pick
elif len(search_query) >= 3:
    st.warning("No instruments matched. Try a different symbol or sync the catalog again.")

if selected_pick:
    st.caption(
        f"Selected: **{selected_pick.trading_symbol}** · `{selected_pick.instrument_key}` · "
        f"{selected_pick.exchange} / {selected_pick.segment}"
    )

analyze_clicked = st.button("Analyze OI", type="primary")

if analyze_clicked:
    if not selected_pick:
        st.error("Search and select an instrument first.")
        st.stop()
    if not token:
        st.error("No Upstox access token on file.")
        st.stop()

    with st.spinner(f"Fetching live OI parameters for {selected_pick.trading_symbol}…"):
        result = analyze_and_matrix(
            selected_pick.trading_symbol,
            token,
            base_url,
            instrument_key=selected_pick.instrument_key,
        )

    if result.get("error"):
        st.error(result["error"])
        if result.get("instrument_key"):
            st.caption(f"Instrument key tried: `{result['instrument_key']}`")
        if result.get("warning"):
            st.warning(result["warning"])
    else:
        if result.get("warning"):
            st.warning(result["warning"])

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Symbol", result["symbol"])
        m2.metric("LTP", f"{result['ltp']:,.2f}")
        m3.metric("Resistance Ceiling", f"{result['resistance_level']:,.1f}")
        m4.metric("Support Floor", f"{result['support_level']:,.1f}")

        c1, c2, c3 = st.columns(3)
        pcr = result["pcr"]
        c1.metric("Put-Call Ratio (PCR)", pcr if isinstance(pcr, str) else f"{pcr:.2f}")
        c2.metric(
            "Nearest Expiry",
            result.get("expiry") or ("Derived (MCX)" if result.get("is_commodity") else "—"),
        )
        c3.metric("Instrument Key", result.get("instrument_key", "—"))

        st.markdown("---")

        if result.get("market_summary"):
            st.info(result["market_summary"])

        st.markdown("#### Dynamic Intraday Trade Engine Matrix")
        df_matrix = pd.DataFrame(result["matrix_data"])
        st.dataframe(
            df_matrix,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Scenario": st.column_config.TextColumn("Scenario", width="small"),
                "Price Action Trigger": st.column_config.TextColumn("Price Action Trigger", width="medium"),
                "Execution Entry Range": st.column_config.TextColumn("Execution Entry Range", width="medium"),
                "OI Condition": st.column_config.TextColumn("OI Condition", width="small"),
                "Tactical Plan": st.column_config.TextColumn("Tactical Plan", width="large"),
            },
        )

        st.caption(
            f"Summary — {result['symbol']} @ {result['ltp']} · "
            f"PCR {pcr} · Ceiling {result['resistance_level']} · Floor {result['support_level']}"
        )

with st.expander("How symbol search works"):
    st.markdown(
        """
        - **Catalog source:** Upstox daily instrument master (NSE, BSE, MCX).
        - **Included:** NSE/BSE equities & indices, MCX futures (for commodities like CRUDEOIL).
        - **Search:** Type 3+ letters — e.g. `REL` → RELIANCE, `CRU` → CRUDEOIL futures, `NIF` → NIFTY indices.
        - **OI chain:** Works on indices and F&O stocks; MCX uses price-derived levels when no option chain exists.
        """
    )
