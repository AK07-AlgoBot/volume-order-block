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

st.markdown("# Admin")
st.caption(
    "Manage global BLR values, broker connections, dashboard users, and strategy access."
)

st.markdown("## BLR values")
st.caption(
    "These levels are global. Saving updates every user's dashboard and the breakout engine "
    "without a server restart."
)
blr_index = st.selectbox("Index", ["NIFTY", "BANKNIFTY", "SENSEX"], key="admin_blr_index")
blr_state: dict = {}
try:
    blr_response = api_request("GET", f"/api/admin/blr?index_code={blr_index}")
    if blr_response.status_code == 200:
        blr_state = blr_response.json().get("state") or {}
    else:
        st.error(blr_response.text or f"BLR API error {blr_response.status_code}")
except Exception as exc:
    st.error(f"Could not load BLR values: {exc}")

if blr_state.get("mid") is None:
    st.info("BLR is not available yet for this index. Wait for the engine to publish today's levels.")
else:
    with st.form(f"admin_blr_form_{blr_index}"):
        c1, c2, c3 = st.columns(3)
        green = c1.number_input(
            "Green",
            min_value=0.01,
            value=float(blr_state.get("green") or 0),
            step=0.05,
            format="%.2f",
        )
        mid = c2.number_input(
            "Mid",
            min_value=0.01,
            value=float(blr_state.get("mid") or 0),
            step=0.05,
            format="%.2f",
        )
        red = c3.number_input(
            "Red",
            min_value=0.01,
            value=float(blr_state.get("red") or 0),
            step=0.05,
            format="%.2f",
        )
        save_blr = st.form_submit_button("Update BLR for everyone", type="primary")
    source = str(blr_state.get("session_open_source") or "unknown")
    updated = str(blr_state.get("admin_updated_at") or blr_state.get("updated_at") or "")
    st.caption(f"Source: `{source}`" + (f" · Updated: {updated}" if updated else ""))
    if save_blr:
        response = api_request(
            "POST",
            "/api/admin/blr",
            json={
                "index_code": blr_index,
                "green": green,
                "mid": mid,
                "red": red,
            },
        )
        if response.status_code == 200:
            st.success("BLR updated globally. The engine will hot-load it on its next cycle.")
            st.rerun()
        else:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            st.error(str(detail))

st.markdown("---")
status_title, status_action = st.columns([4, 1])
with status_title:
    st.markdown("## Broker connections")
    st.caption("Live token validation for each user's selected broker.")
with status_action:
    refresh_status = st.button("Refresh status", use_container_width=True)

if refresh_status or "admin_broker_statuses" not in st.session_state:
    try:
        with st.spinner("Checking broker tokens…"):
            status_response = api_request("GET", "/api/admin/broker-status")
        if status_response.status_code == 200:
            st.session_state["admin_broker_statuses"] = status_response.json().get("statuses") or []
        else:
            st.error(status_response.text or f"Broker status API error {status_response.status_code}")
    except Exception as exc:
        st.error(f"Could not check broker connections: {exc}")

statuses = st.session_state.get("admin_broker_statuses") or []
if statuses:
    status_rows = []
    for row in statuses:
        if row.get("connected"):
            status = "🟢 Connected"
            if row.get("updated_today"):
                status += " · updated today"
        else:
            status = f"🔴 Not connected · {row.get('detail') or 'token invalid'}"
        status_rows.append(
            {
                "User name": row.get("username") or "",
                "Broker": str(row.get("broker") or "").title(),
                "Egress IP": row.get("egress_ip") or "primary",
                "Status": status,
            }
        )
    st.dataframe(status_rows, use_container_width=True, hide_index=True)
else:
    st.info("No broker connection results.")

st.markdown("---")
st.markdown("## User management")

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

st.markdown("### Existing users")
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
st.markdown("### Create user")

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
