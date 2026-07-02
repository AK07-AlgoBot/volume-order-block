"""Admin — create users and assign strategy entitlements."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.constants import ALL_STRATEGIES, STRATEGY_LABELS
from app.ui.auth_session import api_request, is_admin, require_login
from app.ui.styles import inject_dark_theme

inject_dark_theme()
require_login()

if not is_admin():
    st.error("Admin access required.")
    st.stop()

st.markdown("# Admin — Users")
st.caption(
    "Create dashboard users and choose which strategies each user can see. "
    "Telegram trade alerts are **admin-only** (AK07 channel) for now."
)

try:
    resp = api_request("GET", "/api/admin/users")
    if resp.status_code != 200:
        st.error(resp.text or f"API error {resp.status_code}")
        st.stop()
    payload = resp.json()
except Exception as exc:
    st.error(f"Could not load users: {exc}")
    st.stop()

users = payload.get("users") or []
strategy_options = {s["id"]: s["label"] for s in (payload.get("strategies") or [])}

st.markdown("## Existing users")
if not users:
    st.info("No users yet.")
else:
    for row in users:
        u = row.get("username", "?")
        role = row.get("role", "user")
        prof = row.get("profile") or {}
        strategies = prof.get("enabled_strategies") or []
        st.markdown(f"**{u}** · `{role}` · broker `{prof.get('broker', 'upstox')}` · paper `{prof.get('paper_trading')}`")
        if role == "admin":
            st.caption("All strategies (admin) · Telegram alerts enabled")
        else:
            labels = [STRATEGY_LABELS.get(s, s) for s in strategies]
            st.caption(
                "Strategies: " + (", ".join(labels) if labels else "none") + " · Telegram: off"
            )

st.markdown("---")
st.markdown("## Create user")

with st.form("create_user_form"):
    c1, c2 = st.columns(2)
    with c1:
        new_username = st.text_input("Username", max_chars=32)
        new_password = st.text_input("Temporary password", type="password")
    with c2:
        new_role = st.selectbox("Role", ["user", "admin"], index=0)
        new_broker = st.selectbox("Default broker", ["upstox", "kite", "groww"], index=0)
    new_paper = st.checkbox("Paper trading default", value=True)
    picked = st.multiselect(
        "Enabled strategies",
        options=list(strategy_options.keys()),
        default=[ALL_STRATEGIES[2]],
        format_func=lambda x: strategy_options.get(x, x),
    )
    submit = st.form_submit_button("Create user", type="primary")

if submit:
    if not new_username.strip() or len(new_password) < 8:
        st.error("Username required; password must be at least 8 characters.")
    else:
        body = {
            "username": new_username.strip(),
            "password": new_password,
            "role": new_role,
            "enabled_strategies": picked,
            "broker": new_broker,
            "paper_trading": new_paper,
        }
        r = api_request("POST", "/api/admin/users", json=body)
        if r.status_code == 200:
            st.success(f"User {new_username.strip()} created.")
            st.rerun()
        else:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            st.error(str(detail))
