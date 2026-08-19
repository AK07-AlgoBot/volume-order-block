"""Compact sidebar + top status bar for the execution cockpit."""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from app.config.paths import repo_root
from app.constants import (
    STRATEGY_GAMMA,
    STRATEGY_PILL_SHORT,
    STRATEGY_S1_OI,
    STRATEGY_S2_SMC,
    STRATEGY_S3_BREAKOUT,
    STRATEGY_S8_CHOCH,
    STRATEGY_S29_ORB,
    STRATEGY_GC_OF,
    STRATEGY_COPY_KITE,
)
from app.services import cache_manager
from app.services.broker_pnl_store import (
    broker_pnl_label,
    format_pnl_inr,
    get_user_broker_pnl,
    refresh_groww_pnl_if_stale,
)
from app.services.upstox_engine import emergency_square_off_all, release_kill_switch
from app.ui.styles import status_pill

IST = ZoneInfo("Asia/Kolkata")


def brand_logo_path() -> Path:
    return repo_root() / "assets" / "branding" / "ak07_instagram_profile_logo.png"


def _brand_logo_data_uri() -> str:
    logo = brand_logo_path()
    if not logo.is_file():
        return ""
    try:
        raw = base64.b64encode(logo.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:image/png;base64,{raw}"


def render_sidebar_brand() -> None:
    """Brand at top of left panel via st.logo; wordmark comes from theme CSS (::after)."""
    logo = brand_logo_path()
    if logo.is_file():
        st.logo(str(logo), size="large")


def render_brand_topbar(
    *,
    username: str = "",
    production_domain: str = "",
    broker: str = "upstox",
    admin_mode: bool = False,
) -> None:
    """Slim utility chips + mobile page navbar (desktop brand lives in the sidebar)."""
    domain = (production_domain or os.environ.get("PRODUCTION_DOMAIN") or "ak07.in").strip()
    now = datetime.now(IST).strftime("%d %b %Y")
    user = (username or "U").strip() or "U"
    initial = user[:1].upper()
    broker_label = {"upstox": "Upstox", "kite": "Kite", "groww": "Groww"}.get(
        (broker or "upstox").strip().lower(), "Broker"
    )
    nav_links = [
        ("/", "Dashboard"),
        ("/Performance_Review", "Performance"),
        ("/Token_Update", "Token"),
        ("/Stock_OI_Scanner", "OI Scanner"),
    ]
    if admin_mode:
        nav_links.extend(
            [
                ("/Deploy", "Deploy"),
                ("/Admin_Users", "Users"),
                ("/Broker_Settings", "Broker"),
            ]
        )
    nav_html = "".join(
        f'<a class="ak07-mobile-nav-link" href="{href}" target="_self">{label}</a>'
        for href, label in nav_links
    )
    st.markdown(
        f"""
<div class="ak07-topbar">
  <div class="ak07-topbar-brand" aria-hidden="true">AK07</div>
  <div class="ak07-topbar-meta">
    <span class="ak07-topbar-chip">🕒 {now}</span>
    <span class="ak07-topbar-chip"><span class="dot"></span>{domain}</span>
    <span class="ak07-topbar-chip accent"><a href="/Token_Update" target="_self">Broker connect · {broker_label}</a></span>
    <span class="ak07-topbar-avatar" title="{user}">{initial}</span>
  </div>
</div>
<nav class="ak07-mobile-nav" aria-label="Main pages">{nav_html}</nav>
""",
        unsafe_allow_html=True,
    )


def _format_inr_plain(value: float | None) -> str:
    if value is None:
        return "—"
    return f"₹{float(value):,.2f}"


def render_funds_summary(*, username: str = "", broker: str = "upstox") -> None:
    """Available capital + today's P&L card (per logged-in user's broker)."""
    broker_key = (broker or "upstox").strip().lower()
    capital: float | None = None
    day_pnl: float | None = None

    cache_key = f"_ak07_funds_cache_{username}_{broker_key}"
    cached = st.session_state.get(cache_key)
    now_m = datetime.now(IST).timestamp()
    if isinstance(cached, dict) and now_m - float(cached.get("at") or 0) < 30:
        capital = cached.get("capital")
        day_pnl = cached.get("day_pnl")
    else:
        try:
            if broker_key == "groww" and username:
                from app.services.groww_engine import GrowwClient

                snap = refresh_groww_pnl_if_stale(username)
                if snap.get("total_pnl_inr") is not None:
                    day_pnl = float(snap["total_pnl_inr"])
                client = GrowwClient(username)
                capital = client.get_available_margin()
                if day_pnl is None:
                    pnl = client.get_fno_day_pnl()
                    if pnl is not None:
                        day_pnl = float(pnl.get("total_pnl") or 0.0)
            elif broker_key == "kite" and username:
                from app.services.kite_engine import build_kite_client

                client = build_kite_client(username)
                capital = client.get_available_margin()
                pnl = client.get_portfolio_day_pnl()
                if pnl is not None:
                    day_pnl = float(pnl.get("total_pnl") or 0.0)
            else:
                snap = get_user_broker_pnl(username, broker_key)
                if snap.get("total_pnl_inr") is not None:
                    day_pnl = float(snap["total_pnl_inr"])
                from app.services.upstox_engine import build_upstox_client

                client = build_upstox_client(username or "AK07")
                capital = client.get_available_margin()
                if day_pnl is None:
                    pnl = client.get_portfolio_day_pnl()
                    if pnl is not None:
                        day_pnl = float(pnl.get("total_pnl") or 0.0)
        except Exception:
            pass
        st.session_state[cache_key] = {
            "at": now_m,
            "capital": capital,
            "day_pnl": day_pnl,
        }

    if day_pnl is None:
        pnl_cls = "muted"
        pnl_txt = "—"
    elif abs(float(day_pnl)) < 0.005:
        pnl_cls = "muted"
        pnl_txt = "₹0.00"
    elif float(day_pnl) > 0:
        pnl_cls = "win"
        pnl_txt = f"₹{float(day_pnl):+,.2f}"
    else:
        pnl_cls = "loss"
        pnl_txt = f"₹{float(day_pnl):+,.2f}"

    cap_txt = _format_inr_plain(capital if capital is None else float(capital))
    cap_cls = "muted" if capital is None else ""

    st.markdown(
        f"""
<div class="ak07-funds-card">
  <div class="ak07-funds-cell">
    <div class="ak07-funds-ico">₹</div>
    <div>
      <p class="lbl">Available capital</p>
      <p class="val {cap_cls}">{cap_txt}</p>
    </div>
  </div>
  <div class="ak07-funds-cell">
    <div>
      <p class="lbl">Today's P&amp;L</p>
      <p class="val {pnl_cls}">{pnl_txt}</p>
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def render_app_chrome() -> None:
    """Shared chrome: sidebar brand + slim utility strip (funds card is Dashboard-only)."""
    from app.ui import auth_session as auth

    from app.ui.styles import inject_dark_theme

    inject_dark_theme()
    render_sidebar_brand()
    profile = auth.current_profile()
    broker = str(profile.get("broker") or "upstox")
    render_brand_topbar(
        username=auth.current_username(),
        production_domain=(os.environ.get("PRODUCTION_DOMAIN") or "").strip() or "ak07.in",
        broker=broker,
        admin_mode=auth.is_admin(),
    )


def render_top_status_bar(
    *,
    mock_mode: bool,
    production_domain: str,
    refresh_seconds: int,
    can_view_strategy: Callable[[str], bool] | None = None,
    username: str = "",
    broker: str = "upstox",
) -> bool:
    """One-line engine status + auto-refresh toggle. Returns auto_refresh flag."""
    can_view = can_view_strategy or (lambda _sid: True)

    heartbeat = cache_manager.get_json(cache_manager.ENGINE_HEARTBEAT_KEY)
    smc_hb = cache_manager.get_json(cache_manager.SMC_CRT_HEARTBEAT_KEY)
    bo_hb = cache_manager.get_json(cache_manager.BREAKOUT_HEARTBEAT_KEY)
    gamma_hb = cache_manager.get_json(cache_manager.GAMMA_HEARTBEAT_KEY)
    s29_state = cache_manager.get_json(cache_manager.S29_STATE_KEY)
    gc_state = cache_manager.get_json(cache_manager.GC_STATE_KEY)
    copy_state = cache_manager.get_json(cache_manager.COPY_KITE_STATE_KEY)
    choch_state = cache_manager.get_json(cache_manager.CHOCH_STATE_KEY)
    system_bias = cache_manager.get_system_bias()

    s1_mode = "PAPER" if (heartbeat or {}).get("paper_trading") else "LIVE"
    s1_detail = s1_mode if heartbeat else "offline"
    smc_detail = f"→{smc_hb.get('session_end_ist', '15:30')}" if smc_hb else "offline"
    bo_detail = f"→{bo_hb.get('session_end_ist', '15:30')}" if bo_hb else "offline"
    gamma_detail = "expiry" if (gamma_hb or {}).get("expiry_today") else "idle"
    gamma_detail = gamma_detail if gamma_hb else "offline"
    if s29_state:
        s29_detail = "paper" if s29_state.get("paper_trading") else "live"
    else:
        s29_detail = "offline"
    if gc_state:
        gc_detail = "paper" if gc_state.get("paper_trading") else "live"
    else:
        gc_detail = "offline"
    if copy_state:
        copy_detail = "paper" if copy_state.get("paper_trading") else "live"
    else:
        copy_detail = "offline"
    choch_detail = "live" if choch_state else "offline"

    upstox_pnl = cache_manager.get_json(cache_manager.UPSTOX_DAILY_PNL_KEY) or {}
    upstox_total = upstox_pnl.get("total_pnl_inr")
    broker_key = (broker or "upstox").strip().lower()
    pnl_label = broker_pnl_label(broker_key)
    if broker_key == "groww" and username:
        pnl_snap = refresh_groww_pnl_if_stale(username)
    else:
        pnl_snap = get_user_broker_pnl(username, broker_key)
    total_pnl = pnl_snap.get("total_pnl_inr")
    if total_pnl is not None:
        target_pill = status_pill(pnl_label, True, f"Rs.{float(total_pnl):+,.0f}")
    elif upstox_total is not None and broker_key == "upstox":
        target_pill = status_pill("Upstox P&L", True, f"Rs.{float(upstox_total):+,.0f}")
    else:
        target_pill = status_pill(pnl_label, False, "pending")

    pill_specs: list[tuple[str, str, bool, str]] = []
    if can_view(STRATEGY_S1_OI):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S1_OI], STRATEGY_S1_OI, bool(heartbeat), s1_detail))
    if can_view(STRATEGY_S2_SMC):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S2_SMC], STRATEGY_S2_SMC, bool(smc_hb), smc_detail))
    if can_view(STRATEGY_S3_BREAKOUT):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S3_BREAKOUT], STRATEGY_S3_BREAKOUT, bool(bo_hb), bo_detail))
    if can_view(STRATEGY_S29_ORB):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_S29_ORB], STRATEGY_S29_ORB, bool(s29_state), s29_detail))
    if can_view(STRATEGY_GC_OF):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_GC_OF], STRATEGY_GC_OF, bool(gc_state), gc_detail))
    if can_view(STRATEGY_COPY_KITE):
        pill_specs.append((STRATEGY_PILL_SHORT[STRATEGY_COPY_KITE], STRATEGY_COPY_KITE, bool(copy_state), copy_detail))
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

    # Wide status row + refresh; CSS stacks these on narrow screens.
    bar_col, refresh_col = st.columns([11, 1.2], gap="small")
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
