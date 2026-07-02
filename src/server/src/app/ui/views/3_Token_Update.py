"""Daily broker token update — Upstox or Kite based on user profile."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import streamlit.components.v1 as components

IST = ZoneInfo("Asia/Kolkata")

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.config.paths import ensure_repo_and_lib_on_path
from app.services.kite_oauth import kite_redirect_url
from app.ui.auth_session import api_base_url, api_request, cockpit_origin, current_profile, current_username, is_admin, require_login
from app.ui.styles import inject_dark_theme

ensure_repo_and_lib_on_path()

KITE_CONNECT_KEY = "kite_connect_url"
KITE_OPENED_KEY = "_kite_tab_opened"

inject_dark_theme()
require_login()

USERNAME = current_username()
PROFILE = current_profile()
DEFAULT_BROKER = str(PROFILE.get("broker") or "upstox").strip().lower()
PRODUCTION_DOMAIN = (os.environ.get("PRODUCTION_DOMAIN") or "").strip() or "ak07.in"

if is_admin():
    broker = st.sidebar.selectbox(
        "Broker (admin override)",
        ["upstox", "kite", "groww"],
        index={"upstox": 0, "kite": 1, "groww": 2}.get(DEFAULT_BROKER, 0),
    )
else:
    broker = DEFAULT_BROKER
    st.sidebar.caption(f"Your broker: **{broker}**")

if broker == "groww":
    st.markdown("# 🔑 Token Update")
    st.warning("Groww integration is not available yet.")
    st.stop()


def _flash_result(ok: bool, msg: str) -> None:
    if ok:
        st.success(msg)
    else:
        st.error(msg)


def _read_creds() -> dict[str, str]:
    if broker == "kite":
        from kite_credentials_store import read_credentials_file_for_user

        return read_credentials_file_for_user(USERNAME)
    from upstox_credentials_store import read_credentials_file_for_user

    return read_credentials_file_for_user(USERNAME)


def _save_via_api(api_key: str, access_token: str, api_secret: str, base_url: str) -> tuple[bool, str]:
    try:
        r = api_request(
            "POST",
            "/api/settings/credentials",
            json={
                "broker": broker,
                "api_key": api_key,
                "access_token": access_token,
                "api_secret": api_secret,
                "base_url": base_url,
            },
        )
    except Exception as exc:
        return False, str(exc)
    if r.status_code == 200:
        return True, "Saved."
    try:
        return False, str(r.json().get("detail", r.text))
    except Exception:
        return False, r.text


def _test_kite(api_key: str, token: str, base_url: str) -> tuple[bool, str]:
    if not api_key.strip():
        return False, "Kite api_key is required."
    if not token.strip():
        return False, "No access token saved yet — use Login to Zerodha."
    url = f"{base_url.rstrip('/')}/user/profile"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"token {api_key.strip()}:{token.strip()}", "Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, str(exc)
    if resp.status_code == 200:
        profile = resp.json().get("data", {}) if resp.text else {}
        return True, f"Valid — {profile.get('user_name') or profile.get('user_id') or 'connected'}"
    try:
        msg = str(resp.json().get("message") or resp.text)[:200]
    except Exception:
        msg = resp.text[:200]
    return False, f"HTTP {resp.status_code} — {msg}"


def _test_upstox(token: str, base_url: str) -> tuple[bool, str]:
    url = f"{base_url.rstrip('/')}/user/profile"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, str(exc)
    if resp.status_code == 200:
        profile = resp.json().get("data", {}) if resp.text else {}
        return True, f"Valid — {profile.get('user_name') or profile.get('user_id') or 'connected'}"
    return False, f"HTTP {resp.status_code}"


# OAuth return messages
_kite_flag = st.query_params.get("kite")
if _kite_flag == "connected":
    st.session_state.pop(KITE_CONNECT_KEY, None)
    st.session_state.pop(KITE_OPENED_KEY, None)
    st.success("Zerodha connected — access token saved automatically.")
elif _kite_flag == "error":
    st.session_state.pop(KITE_CONNECT_KEY, None)
    st.session_state.pop(KITE_OPENED_KEY, None)
    st.error(f"Zerodha login failed: {st.query_params.get('msg', 'unknown error')}")

broker_label = "Kite (Zerodha)" if broker == "kite" else "Upstox"
st.markdown("# 🔑 Token Update")
st.caption(f"**{broker_label}** · user **{USERNAME}** · paper **{PROFILE.get('paper_trading', True)}**")

creds = _read_creds()
default_base = creds.get("base_url") or (
    "https://api.kite.trade" if broker == "kite" else "https://api.upstox.com/v2"
)

if broker == "kite":
    st.markdown("### Step 1 — App credentials (one-time)")
    st.caption(
        "From your [Kite Personal app](https://developers.kite.trade/). "
        f"Register redirect URL: `{kite_redirect_url()}`"
    )
    with st.form("kite_app_creds"):
        k1, k2 = st.columns(2)
        with k1:
            api_key_in = st.text_input("Kite api_key", value="", placeholder="Leave blank to keep saved key")
        with k2:
            api_secret_in = st.text_input(
                "Kite api_secret",
                value="",
                type="password",
                placeholder="Leave blank to keep saved secret",
            )
        save_app = st.form_submit_button("💾 Save api_key + secret", type="primary")
    if save_app:
        body_key = api_key_in.strip() or creds.get("api_key", "")
        body_secret = api_secret_in.strip() or creds.get("api_secret", "")
        if not body_key or not body_secret:
            st.error("Both api_key and api_secret are required the first time.")
        else:
            ok, msg = _save_via_api(body_key, creds.get("access_token", ""), body_secret, default_base)
            _flash_result(ok, msg)
            if ok:
                time.sleep(0.3)
                st.rerun()

    creds = _read_creds()
    has_app = bool(creds.get("api_key") and creds.get("api_secret"))
    has_token = bool(creds.get("access_token"))

    st.markdown("### Step 2 — Login to Zerodha (daily)")
    if not has_app:
        st.warning("Complete Step 1 first — save api_key and api_secret.")
    elif has_token:
        st.session_state.pop(KITE_CONNECT_KEY, None)
        st.session_state.pop(KITE_OPENED_KEY, None)
        st.success("Kite session is active. Re-login only if **Test Kite connection** fails tomorrow.")
        if st.button("🔄 Re-login to Zerodha", use_container_width=True):
            try:
                r = api_request(
                    "POST",
                    "/api/brokers/kite/connect/start",
                    json={"cockpit_url": cockpit_origin()},
                )
            except Exception as exc:
                st.error(f"API error: {exc}")
            else:
                if r.status_code != 200:
                    try:
                        st.error(r.json().get("detail", r.text))
                    except Exception:
                        st.error(r.text)
                elif not r.json().get("connect_url"):
                    st.error("API did not return a connect URL.")
                else:
                    st.session_state[KITE_CONNECT_KEY] = r.json()["connect_url"]
                    st.session_state.pop(KITE_OPENED_KEY, None)
                    st.rerun()
    else:
        st.caption(
            "One click opens Zerodha in a new tab (User ID + password + TOTP). "
            "When done, you return here automatically."
        )
        pending_url = st.session_state.get(KITE_CONNECT_KEY)
        if pending_url:
            if not st.session_state.get(KITE_OPENED_KEY):
                st.session_state[KITE_OPENED_KEY] = True
                components.html(
                    f"""
                    <script>
                    window.open({json.dumps(pending_url)}, "_blank", "noopener,noreferrer");
                    </script>
                    """,
                    height=0,
                    width=0,
                )
            st.info("Complete Zerodha login in the new tab. Use the button below only if it did not open.")
            st.link_button(
                "Open Zerodha login ↗",
                pending_url,
                type="primary",
                use_container_width=True,
            )
            if st.button("Cancel", use_container_width=True):
                st.session_state.pop(KITE_CONNECT_KEY, None)
                st.session_state.pop(KITE_OPENED_KEY, None)
                st.rerun()
        elif st.button("🔗 Login to Zerodha", type="primary", use_container_width=True):
            try:
                r = api_request(
                    "POST",
                    "/api/brokers/kite/connect/start",
                    json={"cockpit_url": cockpit_origin()},
                )
            except Exception as exc:
                st.error(f"API error: {exc}")
            else:
                if r.status_code != 200:
                    try:
                        st.error(r.json().get("detail", r.text))
                    except Exception:
                        st.error(r.text)
                elif not r.json().get("connect_url"):
                    st.error("API did not return a connect URL.")
                else:
                    st.session_state[KITE_CONNECT_KEY] = r.json()["connect_url"]
                    st.session_state.pop(KITE_OPENED_KEY, None)
                    st.rerun()

    st.markdown("### Status")
    c1, c2 = st.columns(2)
    with c1:
        st.metric("App credentials", "Ready" if has_app else "Missing")
    with c2:
        st.metric("Session token", "Connected" if has_token else "Not connected")

    if st.button("🔍 Test Kite connection"):
        ok, msg = _test_kite(str(creds.get("api_key", "")), str(creds.get("access_token", "")), default_base)
        _flash_result(ok, msg)

    with st.expander("Advanced — paste access_token manually"):
        st.caption("Only if automatic login fails.")
        manual_token = st.text_input("access_token", type="password")
        if st.button("Save manual token"):
            if not manual_token.strip():
                st.warning("Paste a token first.")
            else:
                ok, msg = _save_via_api(
                    str(creds.get("api_key", "")),
                    manual_token.strip(),
                    str(creds.get("api_secret", "")),
                    default_base,
                )
                _flash_result(ok, msg)

else:
    st.caption("Paste Upstox access token or use OAuth login below.")
    current_token = creds.get("access_token", "")
    if current_token:
        st.success(f"Token on file — `•••{current_token[-8:]}`")
    else:
        st.error("No token saved.")

    new_token = st.text_area("Access token", height=100, placeholder="Paste Upstox JWT")
    if st.button("💾 Save Upstox token", type="primary"):
        if not new_token.strip():
            st.warning("Paste a token first.")
        else:
            ok, msg = _save_via_api(
                creds.get("api_key", ""),
                new_token.strip(),
                creds.get("api_secret", ""),
                default_base,
            )
            _flash_result(ok, msg)

    st.markdown("#### Upstox OAuth login")
    upstox_key = st.text_input("Upstox API key", value=creds.get("api_key", ""), type="password")
    redirect_uri = st.text_input("Redirect URI", value="https://127.0.0.1")
    if st.button("🔗 Open Upstox login"):
        if upstox_key.strip():
            url = (
                "https://api.upstox.com/v2/login/authorization/dialog"
                f"?response_type=code&client_id={upstox_key.strip()}&redirect_uri={redirect_uri.strip()}"
            )
            st.markdown(f'[Open Upstox login ↗]({url})')

st.caption(f"API: {api_base_url()} · Redirect (Kite): `{kite_redirect_url()}`")
