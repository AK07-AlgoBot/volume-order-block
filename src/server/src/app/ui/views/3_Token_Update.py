"""Daily broker token update — Upstox / Kite / Groww (guided 2-card layout)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import streamlit.components.v1 as components

IST = ZoneInfo("Asia/Kolkata")

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.config.paths import ensure_repo_and_lib_on_path
from app.services.kite_oauth import kite_redirect_url
from app.ui.auth_session import (
    api_base_url,
    api_request,
    cockpit_origin,
    consume_upstox_oauth_flash,
    create_upstox_oauth_state,
    current_profile,
    current_username,
    is_admin,
    require_login,
    try_complete_upstox_oauth,
)
from app.ui.styles import (
    broker_card_open,
    broker_hero,
    checklist_pills,
    connect_row,
    credentials_saved_box,
    credentials_title,
    help_banner,
    inject_broker_connect_styles,
    inject_dark_theme,
    mask_secret,
    redirect_box,
    status_text,
)

ensure_repo_and_lib_on_path()

KITE_CONNECT_KEY = "kite_connect_url"
KITE_OPENED_KEY = "_kite_tab_opened"

inject_dark_theme()
inject_broker_connect_styles()
require_login()

# Finish Upstox OAuth if user landed here with a pending ?code=
if try_complete_upstox_oauth():
    st.rerun()
consume_upstox_oauth_flash()

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


def _flash_result(ok: bool, msg: str) -> None:
    if ok:
        st.success(msg)
    else:
        st.error(msg)


def _read_creds() -> dict[str, str]:
    if broker == "kite":
        from kite_credentials_store import read_credentials_file_for_user

        return read_credentials_file_for_user(USERNAME)
    if broker == "groww":
        from groww_credentials_store import read_credentials_file_for_user

        return read_credentials_file_for_user(USERNAME)
    from upstox_credentials_store import read_credentials_file_for_user

    return read_credentials_file_for_user(USERNAME)


def _save_via_api(
    api_key: str,
    access_token: str,
    api_secret: str,
    base_url: str,
    *,
    redirect_uri: str = "",
) -> tuple[bool, str]:
    payload = {
        "broker": broker,
        "api_key": api_key,
        "access_token": access_token,
        "api_secret": api_secret,
        "base_url": base_url,
    }
    if redirect_uri.strip():
        payload["redirect_uri"] = redirect_uri.strip()
    try:
        r = api_request("POST", "/api/settings/credentials", json=payload)
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
        return False, "No access token saved yet — use Connect account below."
    url = f"{base_url.rstrip('/')}/user/profile"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"token {api_key.strip()}:{token.strip()}",
                "Accept": "application/json",
            },
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


def _test_groww(token: str, base_url: str) -> tuple[bool, str]:
    if not token.strip():
        return False, "No access token saved yet — generate today's token below."
    url = f"{base_url.rstrip('/')}/v1/user/detail"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token.strip()}",
                "Accept": "application/json",
                "X-API-VERSION": "1.0",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, str(exc)
    try:
        payload = resp.json() if resp.text else {}
    except ValueError:
        payload = {}
    if resp.status_code == 200 and isinstance(payload, dict):
        profile = payload.get("payload") if payload.get("status") == "SUCCESS" else payload
        if isinstance(profile, dict):
            ucc = profile.get("ucc") or profile.get("vendor_user_id") or "connected"
            segments = profile.get("active_segments") or []
            seg_text = ", ".join(segments) if segments else "n/a"
            return True, f"Valid — UCC {ucc} · segments {seg_text}"
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        msg = str(err.get("message") or err)[:200]
    else:
        msg = str(payload.get("message") or resp.text)[:200] if isinstance(payload, dict) else resp.text[:200]
    return False, f"HTTP {resp.status_code} — {msg}"


def _refresh_groww_token(*, auth_mode: str, totp: str = "") -> tuple[bool, str]:
    try:
        r = api_request(
            "POST",
            "/api/brokers/groww/token/refresh",
            json={"auth_mode": auth_mode, "totp": totp},
        )
    except Exception as exc:
        return False, str(exc)
    if r.status_code == 200:
        data = r.json()
        expiry = data.get("token_expiry") or "saved"
        return True, f"Token saved · expires {expiry}"
    try:
        return False, str(r.json().get("detail", r.text))
    except Exception:
        return False, r.text


def _test_upstox(token: str, base_url: str) -> tuple[bool, str]:
    if not token.strip():
        return False, "No access token saved yet."
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


def _copy_button(label: str, value: str, key: str) -> None:
    """Clipboard copy via a tiny HTML button (Streamlit has no native clipboard API)."""
    safe = json.dumps(value)
    components.html(
        f"""
        <button id="{key}" style="
          background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:8px;
          padding:0.4rem 0.85rem;font-weight:600;cursor:pointer;font-size:0.85rem;">
          {label}
        </button>
        <script>
        const btn = document.getElementById("{key}");
        if (btn) {{
          btn.onclick = async () => {{
            try {{
              await navigator.clipboard.writeText({safe});
              btn.innerText = "Copied";
              setTimeout(() => {{ btn.innerText = {json.dumps(label)}; }}, 1200);
            }} catch (e) {{
              btn.innerText = "Copy failed";
            }}
          }};
        }}
        </script>
        """,
        height=42,
    )


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

broker_label = {"kite": "Kite (Zerodha)", "groww": "Groww", "upstox": "Upstox"}.get(
    broker, broker.title()
)

creds = _read_creds()
default_base = creds.get("base_url") or {
    "kite": "https://api.kite.trade",
    "groww": "https://api.groww.in",
    "upstox": "https://api.upstox.com/v2",
}.get(broker, "https://api.upstox.com/v2")

_has_key = bool(str(creds.get("api_key") or "").strip())
_has_secret = bool(str(creds.get("api_secret") or "").strip())
_has_token = bool(str(creds.get("access_token") or "").strip())
if broker == "upstox":
    _cred_ready = _has_key or _has_token
elif broker in ("groww", "kite"):
    _cred_ready = _has_key and _has_secret
else:
    _cred_ready = _has_key and _has_secret

_page_l, _page, _page_r = st.columns([0.25, 3.5, 0.25])
with _page:
    st.markdown(
        broker_hero(
            f"Connect and manage your {broker_label} session",
            "Save API credentials, open broker login, and keep your trading session "
            f"ready for live execution · {USERNAME} · paper {PROFILE.get('paper_trading', True)}",
        ),
        unsafe_allow_html=True,
    )
    st.markdown(
        checklist_pills(
            [
                ("1 · App credentials", "ok" if _cred_ready else "bad"),
                ("2 · Session token", "ok" if _has_token else "warn"),
                ("3 · Daily re-login", "ok" if _has_token else "warn"),
            ]
        ),
        unsafe_allow_html=True,
    )

    # ---------------------------------------------------------------------------
    # GROWW
    # ---------------------------------------------------------------------------
    if broker == "groww":
        edit_key = "edit_groww_creds"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = not _cred_ready

        with st.container(border=True):
            head_l, head_r = st.columns([2.4, 1.2], vertical_alignment="top")
            with head_l:
                st.markdown(
                    broker_card_open(
                        "Groww connection",
                        "API credentials and daily token live here.",
                    ),
                    unsafe_allow_html=True,
                )
            with head_r:
                st.caption("DOCS")
                st.markdown("[Open Groww Trade API ↗](https://groww.in/trade-api)")

            with st.container(border=True):
                t1, t2 = st.columns([4, 1])
                with t1:
                    st.markdown(
                        credentials_title(
                            "Groww API credentials",
                            "Enter your Groww Trade API key and secret to enable trading.",
                        ),
                        unsafe_allow_html=True,
                    )
                with t2:
                    if _cred_ready and not st.session_state[edit_key]:
                        if st.button("Edit", key="groww_edit_btn"):
                            st.session_state[edit_key] = True
                            st.rerun()
                st.markdown(
                    help_banner(
                        "Need API credentials? Open Groww Trade API console.",
                        "Go to Groww Trade API",
                        "https://groww.in/trade-api",
                    ),
                    unsafe_allow_html=True,
                )
                if _cred_ready and not st.session_state[edit_key]:
                    st.markdown(
                        credentials_saved_box(mask_secret(str(creds.get("api_key") or ""))),
                        unsafe_allow_html=True,
                    )
                else:
                    with st.form("groww_app_creds"):
                        groww_key_in = st.text_input(
                            "API KEY",
                            value=str(creds.get("api_key") or ""),
                            placeholder="Your Groww API key",
                        )
                        groww_secret_in = st.text_input(
                            "API SECRET",
                            value=str(creds.get("api_secret") or ""),
                            type="password",
                            placeholder="Your Groww API secret",
                        )
                        b1, b2 = st.columns([1, 1.7])
                        with b1:
                            cancel_groww = st.form_submit_button("Cancel", type="secondary")
                        with b2:
                            save_groww_app = st.form_submit_button(
                                "💾 Save Credentials", type="primary"
                            )
                    if cancel_groww and _cred_ready:
                        st.session_state[edit_key] = False
                        st.rerun()
                    if save_groww_app:
                        body_key = groww_key_in.strip() or creds.get("api_key", "")
                        body_secret = groww_secret_in.strip() or creds.get("api_secret", "")
                        if not body_key or not body_secret:
                            st.error("Both API KEY and API SECRET are required.")
                        else:
                            ok, msg = _save_via_api(
                                body_key,
                                creds.get("access_token", ""),
                                body_secret,
                                default_base,
                            )
                            _flash_result(ok, msg)
                            if ok:
                                st.session_state[edit_key] = False
                                time.sleep(0.3)
                                st.rerun()

        creds = _read_creds()
        has_app = bool(creds.get("api_key") and creds.get("api_secret"))
        has_token = bool(creds.get("access_token"))
        token_expiry = str(creds.get("token_expiry") or "").strip()

        with st.container(border=True):
            st.markdown(
                broker_card_open(
                    "Broker connectivity",
                    "Generate today's Groww access token (expires ~6:00 AM IST).",
                    icon="🔐",
                ),
                unsafe_allow_html=True,
            )
            row_l, row_r = st.columns([2.6, 1.2], vertical_alignment="center")
            with row_l:
                st.markdown(
                    connect_row("Groww", "Required for live order execution."),
                    unsafe_allow_html=True,
                )
            with row_r:
                if not has_app:
                    st.button("Connect account", type="primary", disabled=True, key="groww_connect_dis")
                else:
                    auth_mode = st.radio(
                        "Auth mode",
                        ["approval", "totp"],
                        format_func=lambda x: "App approval" if x == "approval" else "TOTP",
                        horizontal=True,
                        label_visibility="collapsed",
                        key="groww_auth_mode",
                    )
                    totp_code = ""
                    if auth_mode == "totp":
                        totp_code = st.text_input("TOTP", max_chars=8, placeholder="123456")
                    if st.button(
                        "Connect account",
                        type="primary",
                        key="groww_connect",
                        use_container_width=True,
                    ):
                        ok, msg = _refresh_groww_token(auth_mode=auth_mode, totp=totp_code)
                        _flash_result(ok, msg)
                        if ok:
                            time.sleep(0.3)
                            st.rerun()
            st.markdown(
                status_text(has_token, "Session connected", "Not connected"),
                unsafe_allow_html=True,
            )
            if has_token and token_expiry:
                st.caption(f"Token expires **{token_expiry}**")
            st.markdown(
                '<p class="ak07-note">Note: Groww sessions expire daily. '
                "Generate a fresh token each trading morning.</p>",
                unsafe_allow_html=True,
            )
            if has_app and st.button("Test connection", key="groww_test"):
                ok, msg = _test_groww(str(creds.get("access_token", "")), default_base)
                _flash_result(ok, msg)
            with st.expander("Advanced — paste access token manually"):
                manual_token = st.text_input(
                    "access_token", type="password", key="groww_manual_token"
                )
                if st.button("Save manual Groww token"):
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

    # ---------------------------------------------------------------------------
    # KITE
    # ---------------------------------------------------------------------------
    elif broker == "kite":
        redirect = kite_redirect_url()
        edit_key = "edit_kite_creds"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = not _cred_ready

        with st.container(border=True):
            head_l, head_r = st.columns([2.4, 1.2], vertical_alignment="top")
            with head_l:
                st.markdown(
                    broker_card_open(
                        "Zerodha connection",
                        "API credentials and OAuth connection live here.",
                    ),
                    unsafe_allow_html=True,
                )
            with head_r:
                st.caption("REDIRECT URL")
                st.markdown(redirect_box(redirect), unsafe_allow_html=True)
                _copy_button("Copy", redirect, "copy_kite_redirect")

            with st.container(border=True):
                t1, t2 = st.columns([4, 1])
                with t1:
                    st.markdown(
                        credentials_title(
                            "Zerodha Kite API credentials",
                            "Enter your Zerodha Kite Connect API credentials to enable trading.",
                        ),
                        unsafe_allow_html=True,
                    )
                with t2:
                    if _cred_ready and not st.session_state[edit_key]:
                        if st.button("Edit", key="kite_edit_btn"):
                            st.session_state[edit_key] = True
                            st.rerun()
                st.markdown(
                    help_banner(
                        "Need API credentials? Open Kite Developer Console.",
                        "Go to Kite Developer Console",
                        "https://developers.kite.trade/",
                    ),
                    unsafe_allow_html=True,
                )
                if _cred_ready and not st.session_state[edit_key]:
                    st.markdown(
                        credentials_saved_box(mask_secret(str(creds.get("api_key") or ""))),
                        unsafe_allow_html=True,
                    )
                else:
                    with st.form("kite_app_creds"):
                        api_key_in = st.text_input(
                            "API KEY",
                            value=str(creds.get("api_key") or ""),
                            placeholder="Your Kite API key",
                        )
                        api_secret_in = st.text_input(
                            "API SECRET",
                            value=str(creds.get("api_secret") or ""),
                            type="password",
                            placeholder="Your Kite API secret",
                        )
                        b1, b2 = st.columns([1, 1.7])
                        with b1:
                            cancel_kite = st.form_submit_button("Cancel", type="secondary")
                        with b2:
                            save_app = st.form_submit_button(
                                "💾 Save Credentials", type="primary"
                            )
                    if cancel_kite and _cred_ready:
                        st.session_state[edit_key] = False
                        st.rerun()
                    if save_app:
                        body_key = api_key_in.strip() or creds.get("api_key", "")
                        body_secret = api_secret_in.strip() or creds.get("api_secret", "")
                        if not body_key or not body_secret:
                            st.error("Both API KEY and API SECRET are required.")
                        else:
                            ok, msg = _save_via_api(
                                body_key,
                                creds.get("access_token", ""),
                                body_secret,
                                default_base,
                            )
                            _flash_result(ok, msg)
                            if ok:
                                st.session_state[edit_key] = False
                                time.sleep(0.3)
                                st.rerun()

        creds = _read_creds()
        has_app = bool(creds.get("api_key") and creds.get("api_secret"))
        has_token = bool(creds.get("access_token"))

        with st.container(border=True):
            st.markdown(
                broker_card_open(
                    "Broker connectivity",
                    "Connect to Indian exchanges via Zerodha Kite.",
                    icon="🔐",
                ),
                unsafe_allow_html=True,
            )
            row_l, row_r = st.columns([2.6, 1.2], vertical_alignment="center")
            with row_l:
                st.markdown(
                    connect_row(
                        "Zerodha Kite",
                        "Required for trading NFO/MCX instruments.",
                    ),
                    unsafe_allow_html=True,
                )
            with row_r:
                pending_url = st.session_state.get(KITE_CONNECT_KEY)
                if not has_app:
                    st.button(
                        "Connect account",
                        type="primary",
                        disabled=True,
                        key="kite_connect_dis",
                        use_container_width=True,
                    )
                elif pending_url:
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
                    st.link_button(
                        "Open Zerodha login ↗",
                        pending_url,
                        type="primary",
                        use_container_width=True,
                    )
                    if st.button("Cancel", key="kite_oauth_cancel", use_container_width=True):
                        st.session_state.pop(KITE_CONNECT_KEY, None)
                        st.session_state.pop(KITE_OPENED_KEY, None)
                        st.rerun()
                else:

                    def _start_kite_oauth() -> None:
                        try:
                            r = api_request(
                                "POST",
                                "/api/brokers/kite/connect/start",
                                json={"cockpit_url": cockpit_origin()},
                            )
                        except Exception as exc:
                            st.error(f"API error: {exc}")
                            return
                        if r.status_code != 200:
                            try:
                                st.error(r.json().get("detail", r.text))
                            except Exception:
                                st.error(r.text)
                            return
                        if not r.json().get("connect_url"):
                            st.error("API did not return a connect URL.")
                            return
                        st.session_state[KITE_CONNECT_KEY] = r.json()["connect_url"]
                        st.session_state.pop(KITE_OPENED_KEY, None)
                        st.rerun()

                    if st.button(
                        "Connect account",
                        type="primary",
                        key="kite_connect",
                        use_container_width=True,
                    ):
                        _start_kite_oauth()
            st.markdown(
                status_text(has_token, "Session connected", "Not connected"),
                unsafe_allow_html=True,
            )
            st.markdown(
                '<p class="ak07-note">Note: Zerodha sessions expire every day at morning. '
                "You need to re-login once per day to enable automated trading.</p>",
                unsafe_allow_html=True,
            )
            if has_app and st.button("Test connection", key="kite_test"):
                ok, msg = _test_kite(
                    str(creds.get("api_key", "")),
                    str(creds.get("access_token", "")),
                    default_base,
                )
                _flash_result(ok, msg)
            with st.expander("Advanced — paste access token manually"):
                manual_token = st.text_input(
                    "access_token", type="password", key="kite_manual_token"
                )
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

    # ---------------------------------------------------------------------------
    # UPSTOX
    # ---------------------------------------------------------------------------
    else:
        # One-time redirect: saved with credentials; not needed on every Connect.
        mock_mode = os.environ.get("AK07_MOCK") == "1"
        upstox_redirect_default = (
            f"{cockpit_origin()}/"
            if mock_mode
            else (f"https://{PRODUCTION_DOMAIN}/" if PRODUCTION_DOMAIN else f"{cockpit_origin()}/")
        )
        saved_redirect = str(creds.get("redirect_uri") or "").strip()
        if not str(st.session_state.get("upstox_redirect_uri") or "").strip():
            st.session_state["upstox_redirect_uri"] = saved_redirect or upstox_redirect_default
        redirect_uri = str(
            st.session_state.get("upstox_redirect_uri") or saved_redirect or upstox_redirect_default
        ).strip()
        edit_key = "edit_upstox_creds"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = not _has_key

        with st.container(border=True):
            head_l, head_r = st.columns([2.4, 1.2], vertical_alignment="top")
            with head_l:
                st.markdown(
                    broker_card_open(
                        "Upstox connection",
                        "API credentials and OAuth connection live here.",
                    ),
                    unsafe_allow_html=True,
                )
            with head_r:
                st.caption("REDIRECT URI (one-time)")
                st.markdown(
                    redirect_box(redirect_uri or upstox_redirect_default),
                    unsafe_allow_html=True,
                )
                _copy_button(
                    "Copy",
                    redirect_uri or upstox_redirect_default,
                    "copy_upstox_redirect",
                )
            with st.expander("Change redirect URI (only if Upstox app setting changed)"):
                st.caption("Must match the Redirect URI in your Upstox developer app exactly.")
                new_redirect = st.text_input(
                    "Redirect URI",
                    value=redirect_uri or upstox_redirect_default,
                    key="upstox_redirect_uri_edit",
                )
                if st.button("Save redirect URI", key="upstox_save_redirect"):
                    uri_to_save = (new_redirect or "").strip() or upstox_redirect_default
                    st.session_state["upstox_redirect_uri"] = uri_to_save
                    ok, msg = _save_via_api(
                        str(creds.get("api_key") or ""),
                        str(creds.get("access_token") or ""),
                        str(creds.get("api_secret") or ""),
                        default_base,
                        redirect_uri=uri_to_save,
                    )
                    _flash_result(ok, msg if not ok else "Redirect URI saved.")
                    if ok:
                        time.sleep(0.2)
                        st.rerun()

            with st.container(border=True):
                t1, t2 = st.columns([4, 1])
                with t1:
                    st.markdown(
                        credentials_title(
                            "Upstox API credentials",
                            "Enter your Upstox API key and secret to enable trading.",
                        ),
                        unsafe_allow_html=True,
                    )
                with t2:
                    if _has_key and not st.session_state[edit_key]:
                        if st.button("Edit", key="upstox_edit_btn"):
                            st.session_state[edit_key] = True
                            st.rerun()
                st.markdown(
                    help_banner(
                        "Need API credentials? Open Upstox developer apps.",
                        "Go to Upstox developer apps",
                        "https://account.upstox.com/developer/apps",
                    ),
                    unsafe_allow_html=True,
                )
                key_for_oauth = str(creds.get("api_key") or "").strip()
                if _has_key and not st.session_state[edit_key]:
                    st.markdown(
                        credentials_saved_box(mask_secret(key_for_oauth)),
                        unsafe_allow_html=True,
                    )
                else:
                    with st.form("upstox_app_creds"):
                        upstox_key = st.text_input(
                            "API KEY",
                            value=str(creds.get("api_key") or ""),
                            placeholder="Your Upstox API key",
                            key="upstox_api_key_field",
                        )
                        upstox_secret = st.text_input(
                            "API SECRET",
                            value=str(creds.get("api_secret") or ""),
                            type="password",
                            placeholder="Your Upstox API secret (if required)",
                            key="upstox_api_secret_field",
                        )
                        b1, b2 = st.columns([1, 1.7])
                        with b1:
                            cancel_up = st.form_submit_button("Cancel", type="secondary")
                        with b2:
                            save_key = st.form_submit_button(
                                "💾 Save Credentials", type="primary"
                            )
                    if cancel_up and _has_key:
                        st.session_state[edit_key] = False
                        st.rerun()
                    if save_key:
                        body_key = upstox_key.strip() or creds.get("api_key", "")
                        body_secret = upstox_secret.strip() or creds.get("api_secret", "")
                        if not body_key:
                            st.error("API KEY is required.")
                        else:
                            ok, msg = _save_via_api(
                                body_key,
                                str(creds.get("access_token", "")),
                                body_secret,
                                default_base,
                                redirect_uri=redirect_uri,
                            )
                            _flash_result(ok, msg)
                            if ok:
                                st.session_state[edit_key] = False
                                time.sleep(0.3)
                                st.rerun()
                    key_for_oauth = (
                        str(st.session_state.get("upstox_api_key_field") or "").strip()
                        or str(creds.get("api_key") or "").strip()
                    )

        current_token = str(creds.get("access_token") or "")
        uri = redirect_uri or upstox_redirect_default

        with st.container(border=True):
            st.markdown(
                broker_card_open(
                    "Broker connectivity",
                    "Connect to Indian exchanges via Upstox.",
                    icon="🔐",
                ),
                unsafe_allow_html=True,
            )
            row_l, row_r = st.columns([2.6, 1.2], vertical_alignment="center")
            with row_l:
                st.markdown(
                    connect_row("Upstox", "Required for live order execution."),
                    unsafe_allow_html=True,
                )
            upstox_oauth_url = ""
            with row_r:
                if not key_for_oauth:
                    st.button(
                        "Connect account",
                        type="primary",
                        key="upstox_connect_dis",
                        use_container_width=True,
                        disabled=True,
                    )
                    st.caption("Save API key above first.")
                else:
                    # `state` restores AK07 login after Upstox redirect (cookie host mismatch).
                    oauth_state = create_upstox_oauth_state(redirect_uri=uri)
                    oauth_params = {
                        "response_type": "code",
                        "client_id": key_for_oauth,
                        "redirect_uri": uri,
                    }
                    if oauth_state:
                        oauth_params["state"] = oauth_state
                    upstox_oauth_url = (
                        "https://api.upstox.com/v2/login/authorization/dialog?"
                        + urlencode(oauth_params)
                    )
                    st.link_button(
                        "Connect account",
                        upstox_oauth_url,
                        type="primary",
                        use_container_width=True,
                    )
            st.markdown(
                status_text(bool(current_token), "Session connected", "Not connected"),
                unsafe_allow_html=True,
            )
            if current_token:
                st.caption(f"Token on file — `{mask_secret(current_token, keep=8)}`")
            st.markdown(
                '<p class="ak07-note">Note: Upstox access tokens expire — refresh or '
                "re-login before the trading session.</p>",
                unsafe_allow_html=True,
            )
            st.caption(
                "Tip: after Connect account opens Upstox, do **not** refresh the login page — "
                "that drops the OAuth session and OTP often fails with `[1017127]`."
            )
            if st.button("Test connection", key="upstox_test"):
                ok, msg = _test_upstox(current_token, default_base)
                _flash_result(ok, msg)

            # Manual token is the reliable fallback when Upstox OTP/login fails
            with st.expander(
                "If OTP fails — generate token in Upstox console",
                expanded=not bool(current_token),
            ):
                st.markdown(
                    "Error **`[1017127]`** comes from **Upstox login servers** (OTP), "
                    "not from AK07. Use this path instead:"
                )
                st.markdown(
                    "1. Open [Upstox developer apps](https://account.upstox.com/developer/apps)  \n"
                    "2. Open your app → **Generate** access token  \n"
                    "3. Paste the token below and save"
                )
                st.caption(
                    f"OAuth redirect in use: `{uri}` — must match the app’s Redirect URI exactly "
                    "(including trailing `/`)."
                )
                if upstox_oauth_url:
                    with st.expander("Show OAuth URL (debug)"):
                        st.code(upstox_oauth_url, language=None)
                new_token = st.text_area(
                    "Access token",
                    height=100,
                    placeholder="Paste Upstox access token / JWT",
                    key="upstox_manual_token",
                )
                if st.button("Save Upstox token", type="primary", key="upstox_save_manual"):
                    if not new_token.strip():
                        st.warning("Paste a token first.")
                    else:
                        ok, msg = _save_via_api(
                            key_for_oauth or creds.get("api_key", ""),
                            new_token.strip(),
                            creds.get("api_secret", ""),
                            default_base,
                        )
                        _flash_result(ok, msg)
                        if ok:
                            time.sleep(0.3)
                            st.rerun()

    st.caption(f"API: {api_base_url()}")
