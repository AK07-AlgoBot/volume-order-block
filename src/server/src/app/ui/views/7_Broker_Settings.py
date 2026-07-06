"""Broker credentials — Upstox (data) and Kite (orders)."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.config.paths import ensure_repo_and_lib_on_path
from app.ui.auth_session import api_request, current_profile, current_username, is_admin, require_login
from app.ui.styles import inject_dark_theme

ensure_repo_and_lib_on_path()

inject_dark_theme()
require_login()

if not is_admin():
    st.error("Broker settings are admin-only.")
    st.stop()

user = current_username()
profile = current_profile()
default_broker = str(profile.get("broker") or "upstox").strip().lower()
broker_options = ["upstox", "kite", "groww"]
default_idx = broker_options.index(default_broker) if default_broker in broker_options else 0

st.markdown("# Broker settings")
st.caption(
    f"Credentials for **{user}** · assigned broker **{default_broker}** · "
    f"paper **{profile.get('paper_trading', True)}**"
)

broker = st.radio("Broker", broker_options, index=default_idx, horizontal=True)

try:
    meta = api_request("GET", f"/api/settings/credentials?broker={broker}")
    if meta.status_code != 200:
        st.error(meta.text)
        st.stop()
    info = meta.json()
except Exception as exc:
    st.error(f"API unavailable: {exc}")
    st.caption("Ensure the `api` service is running and AK07_API_URL is set.")
    st.stop()

st.info(
    f"Saved file: `{info.get('credentials_path', info.get('credentials_file', ''))}` · "
    f"token {'✓' if info.get('has_access_token') else '—'} · "
    f"api_key {'✓' if info.get('has_api_key') else '—'}"
)

if broker == "kite":
    st.markdown(
        "Daily setup: create a **Personal** app on [developers.kite.trade](https://developers.kite.trade/), "
        "log in via the Kite connect URL, exchange `request_token` for `access_token`, then paste below."
    )
    st.code(
        "https://kite.zerodha.com/connect/login?v=3&api_key=YOUR_API_KEY",
        language=None,
    )
elif broker == "groww":
    st.markdown(
        "Daily setup: generate API key + secret on [Groww Trade API](https://groww.in/trade-api), "
        "then use **Token Update** → **Generate Groww access token** (approve on Groww app). "
        "Token expires daily ~6:00 AM IST."
    )

default_base_urls = {
    "kite": "https://api.kite.trade",
    "groww": "https://api.groww.in",
    "upstox": "https://api.upstox.com/v2",
}

with st.form("broker_cred_form"):
    api_key = st.text_input("API key", value="", placeholder="Leave blank to keep existing")
    access_token = st.text_input("Access token", value="", type="password", placeholder="Paste daily token")
    api_secret = st.text_input("API secret (optional)", value="", type="password", placeholder="For future OAuth")
    base_url = st.text_input(
        "Base URL",
        value=default_base_urls.get(broker, "https://api.upstox.com/v2"),
    )
    save = st.form_submit_button("Save credentials", type="primary")
    test = st.form_submit_button("Test saved credentials")

if save:
    body = {
        "broker": broker,
        "api_key": api_key,
        "access_token": access_token,
        "api_secret": api_secret,
        "base_url": base_url,
    }
    r = api_request("POST", "/api/settings/credentials", json=body)
    if r.status_code == 200:
        st.success("Saved.")
        st.json(r.json())
    else:
        st.error(r.text)

if test:
    r = api_request("POST", f"/api/settings/credentials/test?broker={broker}")
    if r.status_code == 200:
        st.success(r.json().get("message", "OK"))
        st.json(r.json().get("profile", {}))
    else:
        try:
            st.error(r.json().get("detail", r.text))
        except Exception:
            st.error(r.text)
