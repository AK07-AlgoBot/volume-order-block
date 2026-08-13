"""Role-based Streamlit navigation (views/ folder — not auto-discovered by Streamlit)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

_UI_DIR = Path(__file__).resolve().parent
_VIEWS_DIR = _UI_DIR / "views"


def _bootstrap_auth() -> None:
    """Restore session from cookies / resume token before building navigation."""
    from app.ui import auth_session as auth

    # Resume AK07 session from Upstox `state`, then stash ?code= for token exchange.
    auth.capture_upstox_oauth_code_from_query()

    if auth.is_logged_in() or st.session_state.get(auth.LOGOUT_FLAG):
        if auth.is_logged_in() and auth.try_complete_upstox_oauth():
            st.rerun()
        return
    if auth._try_restore_auth():
        if auth.try_complete_upstox_oauth():
            st.rerun()
        st.rerun()
    auth._try_localstorage_bootstrap_once()
    # After localStorage cookie copy + reload, capture may restore session next run.


def build_navigation(*, dashboard_runner) -> st.navigation:
    """Return st.navigation for the current user role."""
    _bootstrap_auth()

    from app.ui.auth_session import (
        consume_upstox_oauth_flash,
        is_admin,
        is_logged_in,
        render_auth_sidebar,
        render_login_page,
    )

    if not is_logged_in():
        return st.navigation(
            [st.Page(render_login_page, title="Sign in", default=True, icon="🔐")],
        )

    consume_upstox_oauth_flash()

    from app.ui.cockpit_layout import render_app_chrome

    # Reset per-run style guards (session persists across reruns; DOM does not).
    st.session_state["_ak07_dark_theme_injected"] = False

    # Sidebar brand + slim top utility strip on every logged-in page.
    render_app_chrome()
    render_auth_sidebar()

    core = [
        st.Page(dashboard_runner, title="Dashboard", default=True, icon="📊"),
        st.Page(_VIEWS_DIR / "2_Performance_Review.py", title="Performance Review", icon="📈"),
        st.Page(_VIEWS_DIR / "3_Token_Update.py", title="Token Update", icon="🔑"),
        st.Page(_VIEWS_DIR / "5_Stock_OI_Scanner.py", title="Stock OI Scanner", icon="🔍"),
    ]
    if not is_admin():
        return st.navigation(core)

    admin_pages = [
        st.Page(_VIEWS_DIR / "4_Deploy.py", title="Deploy", icon="🚀"),
        st.Page(_VIEWS_DIR / "6_Admin_Users.py", title="Admin Users", icon="👥"),
        st.Page(_VIEWS_DIR / "7_Broker_Settings.py", title="Broker Settings", icon="⚙️"),
    ]
    return st.navigation({"Cockpit": core, "Admin": admin_pages})
