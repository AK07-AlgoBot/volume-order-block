"""Compact sidebar + top status bar for the execution cockpit."""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from app.constants import (
    STRATEGY_GAMMA,
    STRATEGY_PILL_SHORT,
    STRATEGY_S1_OI,
    STRATEGY_S2_SMC,
    STRATEGY_S3_BREAKOUT,
    STRATEGY_S7_ORB,
    STRATEGY_S8_CHOCH,
)
from app.services import cache_manager
from app.services.upstox_engine import emergency_square_off_all, release_kill_switch
from app.ui.styles import status_pill


def render_top_status_bar(
    *,
    mock_mode: bool,
    production_domain: str,
    refresh_seconds: int,
    can_view_strategy: Callable[[str], bool] | None = None,
) -> bool:
    """One-line engine status + auto-refresh toggle. Returns auto_refresh flag."""
    can_view = can_view_strategy or (lambda _sid: True)

    heartbeat = cache_manager.get_json(cache_manager.ENGINE_HEARTBEAT_KEY)
    smc_hb = cache_manager.get_json(cache_manager.SMC_CRT_HEARTBEAT_KEY)
    bo_hb = cache_manager.get_json(cache_manager.BREAKOUT_HEARTBEAT_KEY)
    gamma_hb = cache_manager.get_json(cache_manager.GAMMA_HEARTBEAT_KEY)
    s7_state = cache_manager.get_json(cache_manager.S7_STATE_KEY)
    choch_state = cache_manager.get_json(cache_manager.CHOCH_STATE_KEY)
    system_bias = cache_manager.get_system_bias()

    s1_mode = "PAPER" if (heartbeat or {}).get("paper_trading") else "LIVE"
    s1_detail = s1_mode if heartbeat else "offline"
    smc_detail = f"→{smc_hb.get('session_end_ist', '15:30')}" if smc_hb else "offline"
    bo_detail = f"→{bo_hb.get('session_end_ist', '15:30')}" if bo_hb else "offline"
    gamma_detail = "expiry" if (gamma_hb or {}).get("expiry_today") else "idle"
    gamma_detail = gamma_detail if gamma_hb else "offline"
    s7_detail = "live" if s7_state else "offline"
    choch_detail = "live" if choch_state else "offline"

    upstox_pnl = cache_manager.get_json(cache_manager.UPSTOX_DAILY_PNL_KEY) or {}
    upstox_total = upstox_pnl.get("total_pnl_inr")
    if upstox_total is not None:
        target_pill = status_pill("Upstox P&L", True, f"Rs.{float(upstox_total):+,.0f}")
    else:
        target_pill = status_pill("Upstox P&L", False, "pending")

    pill_specs: list[tuple[str, str, bool, str]] = []
    if can_view(STRATEGY_S1_OI):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S1_OI], STRATEGY_S1_OI, bool(heartbeat), s1_detail))
    if can_view(STRATEGY_S2_SMC):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S2_SMC], STRATEGY_S2_SMC, bool(smc_hb), smc_detail))
    if can_view(STRATEGY_S3_BREAKOUT):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S3_BREAKOUT], STRATEGY_S3_BREAKOUT, bool(bo_hb), bo_detail))
    if can_view(STRATEGY_S7_ORB):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S7_ORB], STRATEGY_S7_ORB, bool(s7_state), s7_detail))
    if can_view(STRATEGY_S8_CHOCH):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S8_CHOCH], STRATEGY_S8_CHOCH, bool(choch_state), choch_detail))
    if can_view(STRATEGY_GAMMA):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_GAMMA], STRATEGY_GAMMA, bool(gamma_hb), gamma_detail))

    pills = target_pill
    for short_label, _sid, online, detail in pill_specs:
        pills += " " + status_pill(short_label, online, detail)
    pills += f' <span class="ak07-pill">AI {system_bias}</span>'
    if mock_mode:
        pills += ' <span class="ak07-pill ak07-dot-warn">● MOCK</span>'

    bar_col, refresh_col = st.columns([12, 1], gap="small")
    with bar_col:
        st.markdown(f'<div class="ak07-status-bar">{pills}</div>', unsafe_allow_html=True)
    with refresh_col:
        auto_refresh = st.toggle("Refresh", value=True, key="auto_refresh")
        st.caption(f"{refresh_seconds}s · {production_domain}")
    return auto_refresh


def render_compact_sidebar(*, mock_mode: bool, admin_mode: bool = False) -> None:
    """Minimal sidebar: emergency controls for admin only."""
    with st.sidebar:
        if not admin_mode:
            st.caption("Emergency controls are admin-only.")
            return
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
