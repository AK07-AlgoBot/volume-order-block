"""Role-based Streamlit navigation (views/ folder — not auto-discovered by Streamlit)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

_UI_DIR = Path(__file__).resolve().parent
_VIEWS_DIR = _UI_DIR / "views"


def _bootstrap_auth() -> None:
    """Restore session from cookies / resume token before building navigation."""
    from app.ui import auth_session as auth

    if auth.is_logged_in() or st.session_state.get(auth.LOGOUT_FLAG):
        return
    if auth._try_restore_auth():
        if st.query_params.get("resume"):
            st.rerun()
        return
    auth._try_localstorage_bootstrap_once()


def build_navigation(*, dashboard_runner) -> st.navigation:
    """Return st.navigation for the current user role."""
    _bootstrap_auth()

    from app.ui.auth_session import is_admin, is_logged_in, render_login_page

    if not is_logged_in():
        return st.navigation(
            [st.Page(render_login_page, title="Sign in", default=True, icon="🔐")],
        )

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
