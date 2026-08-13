"""Admin — create users and assign strategy entitlements."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.constants import ALL_STRATEGIES, STRATEGY_LABELS
from app.ui.auth_session import api_request, is_admin, require_login
from app.ui.styles import format_exit_reason_label, inject_dark_theme, summary_chip_row

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
st.markdown("## S3 order history")
st.caption(
    "Closed Strategy 3 trades — entry/exit premium, points, result, and exit reason. "
    "Use period chips like a trading order book."
)

period_labels = {"Today": 1, "7 Days": 7, "30 Days": 30, "60 Days": 60}
period = st.radio(
    "Period",
    list(period_labels.keys()),
    index=1,
    horizontal=True,
    key="admin_s3_period",
    label_visibility="collapsed",
)
s3_days = period_labels[period]
try:
    s3_response = api_request("GET", f"/api/admin/s3-trades?days={s3_days}")
    if s3_response.status_code == 200:
        s3_payload = s3_response.json()
        s3_rows = list(s3_payload.get("rows") or [])
        wins = sum(1 for r in s3_rows if str(r.get("Result") or "").upper() == "WIN")
        losses = sum(1 for r in s3_rows if str(r.get("Result") or "").upper() == "LOSS")
        pnl = round(sum(float(r.get("Actual pts") or 0) for r in s3_rows), 2)
        st.markdown(
            summary_chip_row(total=len(s3_rows), wins=wins, losses=losses, pnl_points=pnl),
            unsafe_allow_html=True,
        )
        if s3_rows:
            display_rows = []
            for r in s3_rows:
                row = dict(r)
                row["Exit reason"] = format_exit_reason_label(str(row.get("Exit reason") or ""))
                display_rows.append(row)
            st.dataframe(display_rows, use_container_width=True, hide_index=True)
            st.caption(
                f"{period} · {s3_payload.get('start_date')} → {s3_payload.get('end_date')}"
            )
        else:
            st.info(
                "No S3 closes in this period yet.\n\n"
                "**Next steps:** keep Token Update connected → wait for an S3 exit → "
                "refresh this page. Partial kills and trail exits show under Exit reason."
            )
    else:
        st.error(s3_response.text or f"S3 trade log API error {s3_response.status_code}")
except Exception as exc:
    st.error(f"Could not load S3 trade log: {exc}")

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
        if hasattr(prof, "model_dump"):
            prof = prof.model_dump()
        strategies = prof.get("enabled_strategies") or []
        lots = int(prof.get("lots") or 1)
        egress = str(prof.get("egress_ip") or "").strip() or "primary"
        header = (
            f"{u} · {role} · broker {prof.get('broker', 'upstox')} · "
            f"paper {prof.get('paper_trading')} · lots {lots} · egress {egress}"
        )
        with st.expander(header, expanded=False):
            if role == "admin":
                st.caption("All strategies (admin) · Telegram alerts enabled")
            else:
                labels = [STRATEGY_LABELS.get(s, s) for s in strategies]
                st.caption(
                    "Strategies: " + (", ".join(labels) if labels else "none") + " · Telegram: off"
                )
            with st.form(f"edit_user_{u}"):
                ec1, ec2 = st.columns(2)
                with ec1:
                    edit_broker = st.selectbox(
                        "Broker",
                        ["upstox", "kite", "groww"],
                        index=["upstox", "kite", "groww"].index(
                            str(prof.get("broker") or "upstox")
                            if str(prof.get("broker") or "upstox") in ("upstox", "kite", "groww")
                            else "upstox"
                        ),
                        key=f"edit_broker_{u}",
                    )
                    edit_lots = st.number_input(
                        "Lots (quantity allocation)",
                        min_value=1,
                        max_value=20,
                        value=lots,
                        step=1,
                        key=f"edit_lots_{u}",
                        help="Number of F&O lots per S3 entry for this user.",
                    )
                with ec2:
                    edit_paper = st.checkbox(
                        "Paper trading",
                        value=bool(prof.get("paper_trading")),
                        key=f"edit_paper_{u}",
                    )
                    edit_egress = st.text_input(
                        "Egress IP (blank = primary)",
                        value=str(prof.get("egress_ip") or ""),
                        key=f"edit_egress_{u}",
                        placeholder="e.g. 65.109.255.239",
                    )
                default_strats = [s for s in strategies if s in strategy_options]
                if role == "admin":
                    st.caption("Admin always has all strategies — selection below is informational.")
                    edit_strats = list(ALL_STRATEGIES)
                else:
                    edit_strats = st.multiselect(
                        "Enabled strategies",
                        options=list(strategy_options.keys()),
                        default=default_strats or [ALL_STRATEGIES[2]],
                        format_func=lambda x: strategy_options.get(x, x),
                        key=f"edit_strats_{u}",
                    )
                save_edit = st.form_submit_button("Save configuration", type="primary")
            if save_edit:
                body = {
                    "broker": edit_broker,
                    "paper_trading": edit_paper,
                    "lots": int(edit_lots),
                    "egress_ip": edit_egress.strip(),
                }
                if role != "admin":
                    body["enabled_strategies"] = edit_strats
                r = api_request("PATCH", f"/api/admin/users/{u}/profile", json=body)
                if r.status_code == 200:
                    st.success(f"Updated {u}.")
                    st.rerun()
                else:
                    try:
                        detail = r.json().get("detail", r.text)
                    except Exception:
                        detail = r.text
                    st.error(str(detail))

st.markdown("---")
st.markdown("### Create user")

with st.form("create_user_form"):
    c1, c2 = st.columns(2)
    with c1:
        new_username = st.text_input("Username", max_chars=32)
        new_password = st.text_input("Temporary password", type="password")
        new_lots = st.number_input(
            "Lots (quantity allocation)",
            min_value=1,
            max_value=20,
            value=1,
            step=1,
            help="Number of F&O lots per S3 entry for this user.",
        )
    with c2:
        new_role = st.selectbox("Role", ["user", "admin"], index=0)
        new_broker = st.selectbox("Default broker", ["upstox", "kite", "groww"], index=0)
        new_egress = st.text_input(
            "Egress IP (blank = primary)",
            value="",
            placeholder="e.g. 65.109.255.239",
        )
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
            "lots": int(new_lots),
            "egress_ip": new_egress.strip(),
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
