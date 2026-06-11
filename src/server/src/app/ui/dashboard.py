"""AK07 Execution Cockpit - dark-themed Streamlit dashboard.

Reads everything from Redis (published by `upstox_engine`), so the dashboard
never blocks or couples to the engine process. The sidebar kill switch both
engages the Redis kill flag (which the engine honors on its next tick) and
transmits emergency market square-offs directly to the Upstox API.

Run:  streamlit run src/server/src/app/ui/dashboard.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager
from app.services.smc_crt_engine import SMC_CRT_INSTRUMENTS
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    emergency_square_off_all,
    release_kill_switch,
)

REFRESH_SECONDS = 5
MOCK_MODE = os.environ.get("AK07_MOCK") == "1"
PRODUCTION_DOMAIN = (os.environ.get("PRODUCTION_DOMAIN") or "").strip() or "ak07.in"

if MOCK_MODE:
    from app.services import mock_data

    mock_data.seed()

st.set_page_config(
    page_title="AK07 Execution Cockpit",
    page_icon="\U0001f3af",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .stApp { background-color: #0c0f14; color: #e6e9ef; }
      section[data-testid="stSidebar"] { background-color: #11151c; }
      div[data-testid="stMetric"] {
        background-color: #161b24; border: 1px solid #232b38;
        border-radius: 10px; padding: 14px 16px;
      }
      div[data-testid="stMetric"] label { color: #8b96a8 !important; }
      div[data-testid="stMetricValue"] { color: #e6e9ef !important; }
      .ak07-block {
        border-radius: 8px; padding: 10px 6px; text-align: center;
        font-weight: 600; font-size: 0.85rem; color: #ffffff;
        margin-bottom: 6px;
      }
      .ak07-green { background-color: #14532d; border: 1px solid #22c55e; }
      .ak07-red { background-color: #7f1d1d; border: 1px solid #ef4444; }
      .ak07-gray { background-color: #1f2937; border: 1px solid #4b5563; }
      .ak07-badge {
        display: inline-block; border-radius: 6px; padding: 4px 12px;
        font-weight: 700; letter-spacing: 0.05em;
      }
      .ak07-bull { background: #14532d; color: #4ade80; }
      .ak07-bear { background: #7f1d1d; color: #f87171; }
      .ak07-neutral { background: #1f2937; color: #9ca3af; }
      button[kind="primary"] {
        background-color: #b91c1c !important; border: 2px solid #ef4444 !important;
        color: #fff !important; font-weight: 800 !important; font-size: 1.05rem !important;
        padding: 0.9rem 0.5rem !important; width: 100%;
      }
      /* Readable text on dark background (captions, expanders, signals) */
      .stApp, .stApp p, .stApp li, .stApp label, .stApp span { color: #e6e9ef; }
      [data-testid="stCaptionContainer"], .stCaption {
        color: #b8c4d4 !important;
      }
      [data-testid="stExpander"] {
        background-color: #161b24 !important;
        border: 1px solid #232b38 !important;
        border-radius: 8px !important;
      }
      [data-testid="stExpander"] summary,
      [data-testid="stExpander"] summary span,
      [data-testid="stExpander"] summary p {
        color: #e6e9ef !important;
      }
      [data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
        background-color: #12161d !important;
        color: #e6e9ef !important;
      }
      [data-testid="stExpander"] div[data-testid="stExpanderDetails"] p,
      [data-testid="stExpander"] div[data-testid="stExpanderDetails"] pre {
        color: #e6e9ef !important;
      }
      .ak07-signal-line {
        color: #e6e9ef; font-family: ui-monospace, monospace;
        font-size: 0.88rem; margin: 0.15rem 0;
      }
      .ak07-muted-line { color: #b8c4d4; font-size: 0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def bias_badge(bias: str) -> str:
    css = {"BULLISH": "ak07-bull", "BEARISH": "ak07-bear"}.get(bias, "ak07-neutral")
    return f'<span class="ak07-badge {css}">{bias}</span>'


def component_block(symbol: str, pct: float | None) -> str:
    if pct is None:
        return f'<div class="ak07-block ak07-gray">{symbol}<br>n/a</div>'
    css = "ak07-green" if pct > 0 else ("ak07-red" if pct < 0 else "ak07-gray")
    return f'<div class="ak07-block {css}">{symbol}<br>{pct:+.2f}%</div>'


def fmt(value: float | int | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}" if isinstance(value, float) else f"{value:,}"


def render_smc_crt_strategy_panel(symbol_code: str) -> None:
    """Strategy Type 2 — SMC+CRT block (below AK07 Active Position)."""
    st.markdown("---")
    st.markdown("#### Strategy Type 2 — SMC + CRT")

    smc = cache_manager.get_json(cache_manager.SMC_CRT_STATE_KEY_TEMPLATE.format(symbol=symbol_code))
    smc_hb = cache_manager.get_json(cache_manager.SMC_CRT_HEARTBEAT_KEY)

    if not smc and not smc_hb:
        st.caption("SMC+CRT engine offline — start `smc_crt_engine` service or MOCK mode.")
        return

    if smc_hb:
        end = smc_hb.get("session_end_ist", "23:30")
        mode = "PAPER" if smc_hb.get("paper_trading") else "LIVE"
        st.caption(f"SMC engine heartbeat {str(smc_hb.get('at', ''))[:19]} · session until {end} IST · {mode}")

    if not smc:
        st.info(f"No SMC state for {symbol_code} yet.")
        return

    cfg = SMC_CRT_INSTRUMENTS.get(symbol_code)
    if cfg and cfg.paper_only:
        source = smc.get("quote_source") or "unknown"
        key = smc.get("instrument_key") or "—"
        if source == "upstox":
            st.caption(f"Paper orders only · **live Upstox quote** · `{key}`")
        else:
            st.warning(
                "Showing **simulated** prices — Upstox MCX quote unavailable. "
                "Check access token (Profile HTTP 200) and restart `smc_crt_engine`."
            )
            st.caption(f"Optional override: set `SMC_CRT_{symbol_code}_INSTRUMENT_KEY` in `.env`")

    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("CRH (range high)", fmt(smc.get("crh")))
    s2.metric("CRM (equilibrium)", fmt(smc.get("crm")))
    s3.metric("CRL (range low)", fmt(smc.get("crl")))
    s4.metric("Spot", fmt(float(smc["spot"])) if smc.get("spot") is not None else "—")
    setup = str(smc.get("setup_label") or "—")
    s5.metric("Setup", setup[:22] + "…" if len(setup) > 22 else setup)
    if len(setup) > 22:
        st.markdown(f'<p class="ak07-muted-line">Setup detail: {setup}</p>', unsafe_allow_html=True)

    flags = []
    if smc.get("swept_low"):
        flags.append("CRL swept")
    if smc.get("swept_high"):
        flags.append("CRH swept")
    if smc.get("crt_ready"):
        flags.append("CRT locked")
    if smc.get("entries_blocked"):
        flags.append("entries blocked")
    if flags:
        st.markdown(f'<p class="ak07-muted-line">{" · ".join(flags)}</p>', unsafe_allow_html=True)

    fvg = smc.get("fvg") or {}
    if fvg:
        fvg_line = (
            f"Last FVG: {fvg.get('direction', '—')} "
            f"{fmt(float(fvg.get('low'))) if fvg.get('low') is not None else '—'} – "
            f"{fmt(float(fvg.get('high'))) if fvg.get('high') is not None else '—'}"
        )
        st.markdown(f'<p class="ak07-muted-line">{fvg_line}</p>', unsafe_allow_html=True)

    st.markdown("##### Strategy Type 2 — Active Position")
    smc_pos = smc.get("position")
    if smc_pos:
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Direction", smc_pos.get("direction", "—"))
        p2.metric("Entry", fmt(float(smc_pos.get("entry_price", 0))))
        p3.metric("Stop (FVG)", fmt(float(smc_pos.get("sl_price", 0))))
        p4.metric("TP1 CRM", fmt(float(smc_pos.get("tp1_price", 0))))
        p5.metric("TP2 liquidity", fmt(float(smc_pos.get("tp2_price", 0))))
        if smc.get("spot") is not None and smc_pos.get("entry_price") is not None:
            spot = float(smc["spot"])
            entry = float(smc_pos["entry_price"])
            pnl = spot - entry if smc_pos.get("direction") == "LONG" else entry - spot
            st.metric("Live P&L (pts)", f"{pnl:+.2f}")
    else:
        st.caption("Flat — waiting for sweep + FVG + confirmation.")

    signals = smc.get("signals") or []
    if signals:
        with st.expander("Recent SMC signals", expanded=False):
            for line in signals:
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)

    updated = str(smc.get("updated_at", ""))[:19].replace("T", " ")
    st.markdown(
        f'<p class="ak07-muted-line">SMC state updated {updated} · trades today {smc.get("trades_today", 0)}</p>',
        unsafe_allow_html=True,
    )


def render_smc_only_tab(symbol_code: str) -> None:
    """Full-tab SMC+CRT view for commodity paper instruments."""
    cfg = SMC_CRT_INSTRUMENTS[symbol_code]
    st.markdown(f"### {cfg.display}")
    st.caption("Strategy Type 2 · paper orders · live Upstox quotes · session until 23:30 IST")
    render_smc_crt_strategy_panel(symbol_code)


# ---------------------------------------------------------------------------
# Sidebar: engine status + emergency controls
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## \U0001f3af AK07 Cockpit")
    if MOCK_MODE:
        st.info("MOCK DATA MODE — simulated feed, broker APIs disabled")

    heartbeat = cache_manager.get_json(cache_manager.ENGINE_HEARTBEAT_KEY)
    if heartbeat:
        mode = "PAPER" if heartbeat.get("paper_trading") else "LIVE"
        st.success(f"Engine ONLINE ({mode}) — {heartbeat.get('at', '')[:19]}")
    else:
        st.error("Engine OFFLINE — no heartbeat in Redis")

    smc_hb = cache_manager.get_json(cache_manager.SMC_CRT_HEARTBEAT_KEY)
    if smc_hb:
        st.success(f"SMC+CRT ONLINE — until {smc_hb.get('session_end_ist', '23:30')} IST")
    else:
        st.warning("SMC+CRT engine offline")

    system_bias = cache_manager.get_system_bias()
    st.markdown(f"**AI System Bias:** {bias_badge(system_bias)}", unsafe_allow_html=True)

    kill_flag = cache_manager.get_json(cache_manager.KILL_SWITCH_KEY)
    kill_engaged = bool(kill_flag and kill_flag.get("engaged"))

    st.markdown("---")
    st.markdown("### \u26a0\ufe0f Emergency Controls")

    if kill_engaged:
        st.error(f"KILL SWITCH ENGAGED\nsince {str(kill_flag.get('at', ''))[:19]}")
        if st.button("Release kill switch (re-arm engine)"):
            release_kill_switch()
            st.rerun()
    else:
        if st.button("\U0001f6d1 EMERGENCY COCKPIT KILL-SWITCH", type="primary"):
            with st.spinner("Engaging kill switch and squaring off via Upstox..."):
                results = emergency_square_off_all()
            for scope, outcome in results.items():
                st.warning(f"{scope}: {outcome}")

    st.markdown("---")
    auto_refresh = st.toggle("Auto-refresh", value=True, key="auto_refresh")
    st.caption(f"Refreshes every {REFRESH_SECONDS}s while enabled.")
    st.caption(f"Production endpoint: https://{PRODUCTION_DOMAIN}")


# ---------------------------------------------------------------------------
# Main: one tab per index
# ---------------------------------------------------------------------------
st.markdown("# AK07 Multi-Index Execution Cockpit")
st.caption(f"Rendered {datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')} local · Strategy 1 (AK07) + Strategy 2 (SMC+CRT)")

ak07_tabs = [cfg.display for cfg in INDEX_CONFIGS.values()]
smc_only_codes = ["CRUDE", "GOLD", "SILVER"]
smc_tab_labels = [SMC_CRT_INSTRUMENTS[c].display for c in smc_only_codes]
tabs = st.tabs(ak07_tabs + smc_tab_labels)

for tab, (code, cfg) in zip(tabs[: len(ak07_tabs)], INDEX_CONFIGS.items()):
    with tab:
        state = cache_manager.get_json(cache_manager.INDEX_STATE_KEY_TEMPLATE.format(index=code))
        if not state:
            st.info(f"No live state for {cfg.display} yet — is the engine running?")
            continue

        spot = state.get("spot")
        call_wall = state.get("call_wall")
        put_floor = state.get("put_floor")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Live Spot Price", fmt(float(spot)) if spot is not None else "—")
        c2.metric(
            "Institutional Call Wall \u2191",
            fmt(call_wall, 0),
            delta=(f"{call_wall - spot:+,.0f} pts away" if call_wall and spot else None),
            delta_color="off",
        )
        c3.metric(
            "Institutional Put Floor \u2193",
            fmt(put_floor, 0),
            delta=(f"{spot - put_floor:+,.0f} pts above" if put_floor and spot else None),
            delta_color="off",
        )
        with c4:
            st.markdown("**Component Bias**")
            st.markdown(bias_badge(state.get("component_bias", "NEUTRAL")), unsafe_allow_html=True)
            st.caption(f"Trades today: {state.get('trades_today', 0)}/{state.get('max_trades', 2)}")

        st.markdown("#### Institutional Component Health")
        components: dict[str, float | None] = state.get("components") or {}
        if components:
            cols = st.columns(len(components))
            for col, (symbol, pct) in zip(cols, components.items()):
                col.markdown(component_block(symbol, pct), unsafe_allow_html=True)
        else:
            st.caption("Component quotes unavailable.")

        position = state.get("position")
        st.markdown("#### Active Position")
        if position:
            p1, p2, p3, p4, p5, p6 = st.columns(6)
            option = f"{position.get('option_strike', '')}{position.get('option_type', '')}"
            p1.metric("Direction", position.get("direction", "—"), delta=option or None, delta_color="off")
            p2.metric("Entry (spot)", fmt(float(position.get("entry_price", 0))))
            p3.metric("Target", fmt(float(position.get("target_price", 0))))
            p4.metric("Stop-Loss", fmt(float(position.get("sl_price", 0))))
            lots = int(position.get("lots_remaining", 1))
            p5.metric(
                "Lots Running",
                f"{lots}/2",
                delta="1 lot booked" if position.get("partial_booked") else "awaiting +60 book",
                delta_color="off",
            )
            if spot is not None:
                live_pnl = (
                    spot - position["entry_price"]
                    if position.get("direction") == "LONG"
                    else position["entry_price"] - spot
                )
                p6.metric("Live P&L (pts)", f"{live_pnl:+.2f}")
        else:
            st.caption("Flat — no open position.")

        updated = str(state.get("updated_at", ""))[:19].replace("T", " ")
        flags = []
        if state.get("entries_blocked"):
            flags.append("entries blocked")
        elif state.get("monitoring_active"):
            flags.append("monitoring active execution boundaries")
        if state.get("paper_trading"):
            flags.append("paper mode")
        st.caption(f"Engine state updated {updated}" + (f" · {' · '.join(flags)}" if flags else ""))

        smc_symbol = "NIFTY" if code == "NIFTY" else None
        if smc_symbol:
            render_smc_crt_strategy_panel(smc_symbol)
        else:
            st.markdown("---")
            st.markdown("#### Strategy Type 2 — SMC + CRT")
            st.caption("Primary SMC+CRT feed runs on **Nifty 50** tab and commodity tabs (Crude / Gold / Silver).")

for tab, smc_code in zip(tabs[len(ak07_tabs) :], smc_only_codes):
    with tab:
        render_smc_only_tab(smc_code)

if auto_refresh:
    time.sleep(REFRESH_SECONDS)
    st.rerun()
