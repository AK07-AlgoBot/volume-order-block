"""AK07 Token Update — paste a fresh Upstox access token from the browser."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.config.paths import ensure_repo_and_lib_on_path
from app.ui.styles import inject_dark_theme

ensure_repo_and_lib_on_path()

st.set_page_config(
    page_title="AK07 — Token Update",
    page_icon="🔑",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_dark_theme()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store():
    from upstox_credentials_store import (
        credentials_file_for_user,
        mask_tail,
        normalize_access_token,
        persist_credentials_for_user,
        read_credentials_file_for_user,
    )
    return credentials_file_for_user, mask_tail, normalize_access_token, persist_credentials_for_user, read_credentials_file_for_user


USERNAME = "AK07"


def _read() -> dict[str, str]:
    _, _, _, _, read = _store()
    return read(USERNAME)


def _save(token: str) -> dict[str, str]:
    cred_file, _, normalize, persist, read = _store()
    current = read(USERNAME)
    current["access_token"] = normalize(token)
    return persist(USERNAME, current)


def _test_token(token: str, base_url: str) -> tuple[bool, str]:
    """Call Upstox /user/profile. Returns (ok, message)."""
    url = f"{base_url.rstrip('/')}/user/profile"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, f"Network error: {exc}"
    if resp.status_code == 200:
        try:
            profile = resp.json().get("data", {})
            name = profile.get("user_name") or profile.get("email") or "unknown"
            uid = profile.get("user_id", "")
            return True, f"Valid — {name} ({uid})"
        except Exception:
            return True, "Valid (profile parse error)"
    try:
        err = resp.json()
        msg = ""
        for e in err.get("errors", []):
            msg = e.get("message", "") if isinstance(e, dict) else str(e)
            break
        msg = msg or err.get("message", "") or resp.text[:200]
    except Exception:
        msg = resp.text[:200]
    return False, f"HTTP {resp.status_code} — {msg}"


def _jwt_expiry(token: str) -> str | None:
    """Decode JWT exp claim without a library (base64 only)."""
    import base64, json as _json
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp:
            dt = datetime.fromtimestamp(int(exp), tz=IST)
            return dt.strftime("%d %b %Y  %H:%M IST")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.markdown("# 🔑 Token Update")
st.caption("Paste your Upstox access token here — it is saved to disk immediately and the trading engine picks it up on the next tick.")

st.markdown("---")

# ---- Current status --------------------------------------------------------
creds = _read()
current_token = creds.get("access_token", "")
_, mask_tail, _, _, _ = _store()

status_col, meta_col = st.columns([2, 3])

with status_col:
    st.markdown("#### Current Token")
    if current_token:
        st.success(f"Token present — `•••{current_token[-8:]}`")
        exp = _jwt_expiry(current_token)
        if exp:
            st.caption(f"Expires: {exp}")
    else:
        st.error("No token saved — engine cannot trade.")

with meta_col:
    st.markdown("#### Engine User")
    st.info(f"**{USERNAME}**  ·  credentials file: `upstox_credentials.json`")
    api_key = creds.get("api_key", "")
    if api_key:
        st.caption(f"API key on file: `•••{api_key[-6:]}`")
    else:
        st.caption("No API key on file (needed for broker OAuth).")

st.markdown("---")

# ---- Quick-test existing token ---------------------------------------------
st.markdown("#### Test Current Token")
if st.button("🔍 Test saved token against Upstox API", use_container_width=False):
    if not current_token:
        st.error("No token to test.")
    else:
        base_url = creds.get("base_url", "https://api.upstox.com/v2")
        with st.spinner("Calling Upstox /user/profile …"):
            ok, msg = _test_token(current_token, base_url)
        if ok:
            st.success(f"✅ {msg}")
        else:
            st.error(f"❌ {msg}")

st.markdown("---")

# ---- Paste new token -------------------------------------------------------
st.markdown("#### Paste New Access Token")
st.caption(
    "Get your token from the "
    "[Upstox developer console](https://account.upstox.com/developer/apps) "
    "or from the daily login flow. Paste the raw JWT (without the `Bearer ` prefix)."
)

new_token = st.text_area(
    "Access token",
    height=120,
    placeholder="eyJ0eXAiOiJKV1Qi…  (paste full JWT here)",
    label_visibility="collapsed",
)

col_save, col_test, col_clear = st.columns([1, 1, 4])

with col_save:
    save_clicked = st.button("💾 Save Token", type="primary", use_container_width=True)

with col_test:
    test_new_clicked = st.button("🔍 Test First", use_container_width=True)

# Test new token before saving
if test_new_clicked:
    raw = new_token.strip()
    if not raw:
        st.warning("Paste a token above first.")
    else:
        _, _, normalize_access_token, _, _ = _store()
        tok = normalize_access_token(raw)
        base_url = creds.get("base_url", "https://api.upstox.com/v2")
        with st.spinner("Testing …"):
            ok, msg = _test_token(tok, base_url)
        if ok:
            st.success(f"✅ {msg} — token looks good, click **Save Token** to persist it.")
        else:
            st.error(f"❌ {msg}")

# Save token
if save_clicked:
    raw = new_token.strip()
    if not raw:
        st.warning("Nothing to save — paste a token in the box above.")
    else:
        try:
            saved = _save(raw)
            saved_tok = saved.get("access_token", "")
            st.success(f"✅ Token saved — `•••{saved_tok[-8:]}`")
            exp = _jwt_expiry(saved_tok)
            if exp:
                st.caption(f"Token expires: {exp}")
            st.info(
                "The trading engine will pick up the new token automatically on its next "
                "5-minute tick.  No restart required in Docker. "
                "If running locally, the engine reloads credentials from disk on every bar."
            )
            time.sleep(0.5)
            st.rerun()
        except Exception as exc:
            st.error(f"Save failed: {exc}")

st.markdown("---")

# ---- Upstox login helper ---------------------------------------------------
st.markdown("#### Generate Upstox Login URL")
st.caption("If you need a fresh token, use the OAuth flow below.")

api_key_input = st.text_input(
    "Your Upstox API key (client_id)",
    value=creds.get("api_key", ""),
    type="password",
    placeholder="2028e9a0-…",
)
redirect_uri = st.text_input(
    "Redirect URI (must match your Upstox app settings)",
    value="https://127.0.0.1",
    placeholder="https://your-domain.com/callback",
)

if st.button("🔗 Open Upstox login in new tab"):
    if not api_key_input.strip():
        st.warning("Enter your API key first.")
    else:
        login_url = (
            "https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code"
            f"&client_id={api_key_input.strip()}"
            f"&redirect_uri={redirect_uri.strip()}"
        )
        st.markdown(f'<a href="{login_url}" target="_blank">Click here to log in to Upstox ↗</a>', unsafe_allow_html=True)
        st.caption(f"Login URL: `{login_url}`")

st.markdown("---")
st.caption(
    "After Upstox OAuth redirects back, copy the `access_token` from the response "
    "and paste it in the **Paste New Access Token** section above."
)
