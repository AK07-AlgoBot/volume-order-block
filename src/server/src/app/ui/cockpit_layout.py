"""Compact sidebar + top status bar for the execution cockpit."""

from __future__ import annotations

import streamlit as st

from app.services import cache_manager
from app.services.upstox_engine import emergency_square_off_all, release_kill_switch
from app.ui.styles import status_pill


def render_top_status_bar(
    *,
    mock_mode: bool,
    production_domain: str,
    refresh_seconds: int,
) -> bool:
    """One-line engine status + auto-refresh toggle. Returns auto_refresh flag."""
    heartbeat = cache_manager.get_json(cache_manager.ENGINE_HEARTBEAT_KEY)
    smc_hb = cache_manager.get_json(cache_manager.SMC_CRT_HEARTBEAT_KEY)
    bo_hb = cache_manager.get_json(cache_manager.BREAKOUT_HEARTBEAT_KEY)
    system_bias = cache_manager.get_system_bias()

    s1_mode = "PAPER" if (heartbeat or {}).get("paper_trading") else "LIVE"
    s1_detail = s1_mode if heartbeat else "offline"
    smc_detail = f"→{smc_hb.get('session_end_ist', '23:30')}" if smc_hb else "offline"
    bo_detail = f"→{bo_hb.get('session_end_ist', '15:30')}" if bo_hb else "offline"

    pills = " ".join(
        [
            status_pill("S1 OI", bool(heartbeat), s1_detail),
            status_pill("S2 SMC", bool(smc_hb), smc_detail),
            status_pill("S3 BLR", bool(bo_hb), bo_detail),
            f'<span class="ak07-pill">AI {system_bias}</span>',
        ]
    )
    if mock_mode:
        pills += ' <span class="ak07-pill ak07-dot-warn">● MOCK</span>'

    c1, c2 = st.columns([5, 1])
    with c1:
        st.markdown(f'<div class="ak07-status-bar">{pills}</div>', unsafe_allow_html=True)
    with c2:
        auto_refresh = st.toggle("Refresh", value=True, key="auto_refresh")
        st.caption(f"{refresh_seconds}s · {production_domain}")
    return auto_refresh


def render_compact_sidebar(*, mock_mode: bool) -> None:
    """Minimal sidebar: emergency controls only (status lives in top bar)."""
    with st.sidebar:
        st.caption("AK07 · Emergency")
        if mock_mode:
            st.caption("Mock mode — no live orders")

        kill_flag = cache_manager.get_json(cache_manager.KILL_SWITCH_KEY)
        kill_engaged = bool(kill_flag and kill_flag.get("engaged"))

        if kill_engaged:
            st.error(f"KILL SWITCH ON\n{str(kill_flag.get('at', ''))[:19]}")
            if st.button("Release kill switch", use_container_width=True):
                release_kill_switch()
                st.rerun()
        elif st.button("Emergency kill-switch", type="primary", use_container_width=True):
            with st.spinner("Squaring off..."):
                results = emergency_square_off_all()
            for scope, outcome in results.items():
                st.warning(f"{scope}: {outcome}")

        st.caption("Use « to collapse this panel for full-width charts.")
