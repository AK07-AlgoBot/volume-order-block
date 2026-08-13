"""Streamlit session login — session_state + browser cookie + Redis browser session."""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

import requests
import streamlit as st
import streamlit.components.v1 as components

from app.config.settings import get_settings
from app.constants import ADMIN_ROLE
from app.services import cache_manager
from app.services.browser_session import create_session, delete_session, load_session
from app.services.user_profiles_store import ensure_profile
from app.services.users_store import authenticate
from app.utils.security import create_access_token, decode_token

SESSION_TOKEN = "ak07_auth_token"
SESSION_USER = "ak07_username"
SESSION_ROLE = "ak07_role"
SESSION_PROFILE = "ak07_profile"
SESSION_BROWSER_SID = "ak07_browser_sid"
COOKIE_SID = "ak07_browser_sid"
COOKIE_TOKEN = "ak07_auth_token"
COOKIE_USER = "ak07_username"
COOKIE_ROLE = "ak07_role"
LS_BOOTSTRAP_FLAG = "_ak07_ls_bootstrap_done"
LOGOUT_FLAG = "_ak07_logged_out"
UPSTOX_PENDING_CODE = "upstox_oauth_pending_code"
UPSTOX_PENDING_REDIRECT = "upstox_oauth_pending_redirect"
UPSTOX_FLASH = "upstox_oauth_flash"
UPSTOX_STATE_KEY = "ak07:upstox_oauth_state:{state}"
UPSTOX_STATE_TTL = 600


def api_base_url() -> str:
    return (os.environ.get("AK07_API_URL") or "http://localhost:8080").rstrip("/")


def is_logged_in() -> bool:
    return bool(st.session_state.get(SESSION_TOKEN) and st.session_state.get(SESSION_USER))


def _cookie_max_age_seconds() -> int:
    return max(300, get_settings().jwt_expire_minutes * 60)


def _request_cookies() -> dict[str, str]:
    """HTTP cookies from the browser request — available immediately on refresh."""
    try:
        raw = st.context.cookies
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    except Exception:
        return {}


def _set_browser_auth_client_side(sid: str) -> None:
    max_age = _cookie_max_age_seconds()
    components.html(
        f"""
        <script>
        (function () {{
            const doc = window.parent.document;
            const ls = window.parent.localStorage;
            const opts = "path=/; max-age={max_age}; samesite=lax";
            doc.cookie = {json.dumps(COOKIE_SID)} + "=" + encodeURIComponent({json.dumps(sid)}) + "; " + opts;
            ls.setItem({json.dumps(COOKIE_SID)}, {json.dumps(sid)});
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


def _clear_browser_auth_client_side() -> None:
    names = [COOKIE_SID, COOKIE_TOKEN, COOKIE_USER, COOKIE_ROLE]
    components.html(
        f"""
        <script>
        (function () {{
            const doc = window.parent.document;
            const ls = window.parent.localStorage;
            const names = {json.dumps(names)};
            for (const name of names) {{
                doc.cookie = name + "=; path=/; max-age=0; samesite=lax";
                ls.removeItem(name);
            }}
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


def _session_from_token(token: str, username: str, role: str) -> bool:
    payload = decode_token(token)
    if not payload:
        return False
    if str(payload.get("sub") or "").strip() != username:
        return False
    role_norm = str(payload.get("role") or role or "user").strip() or "user"
    st.session_state[SESSION_TOKEN] = token
    st.session_state[SESSION_USER] = username
    st.session_state[SESSION_ROLE] = role_norm
    st.session_state[SESSION_PROFILE] = ensure_profile(username, role=role_norm)
    return True


def _persist_auth_browser(token: str, username: str, role: str) -> None:
    old_sid = str(st.session_state.get(SESSION_BROWSER_SID) or "").strip()
    if old_sid:
        delete_session(old_sid)

    sid = create_session(username, role, token, ttl_seconds=_cookie_max_age_seconds())
    st.session_state[SESSION_BROWSER_SID] = sid
    _set_browser_auth_client_side(sid)


def _cookie_get(cookies: dict[str, Any], name: str) -> str:
    return str(cookies.get(name) or "").strip()


def _clear_stale_browser_sid() -> None:
    """Drop expired browser session id from cookie + localStorage (stops reload loops)."""
    components.html(
        f"""
        <script>
        (function () {{
            const doc = window.parent.document;
            const ls = window.parent.localStorage;
            ls.removeItem({json.dumps(COOKIE_SID)});
            doc.cookie = {json.dumps(COOKIE_SID)} + "=; path=/; max-age=0; samesite=lax";
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


def _restore_from_cookies(cookies: dict[str, Any]) -> bool:
    if is_logged_in() or st.session_state.get(LOGOUT_FLAG) or not cookies:
        return False

    sid = _cookie_get(cookies, COOKIE_SID)
    if sid:
        rec = load_session(sid)
        if rec and _session_from_token(rec["token"], rec["username"], rec["role"]):
            st.session_state[SESSION_BROWSER_SID] = sid
            return True
        delete_session(sid)
        _clear_stale_browser_sid()

    token = _cookie_get(cookies, COOKIE_TOKEN)
    username = _cookie_get(cookies, COOKIE_USER)
    role = _cookie_get(cookies, COOKIE_ROLE) or "user"
    if token and username and _session_from_token(token, username, role):
        return True

    return False


def _try_restore_auth() -> bool:
    if is_logged_in() or st.session_state.get(LOGOUT_FLAG):
        return False

    if try_kite_resume_from_query():
        return True

    return _restore_from_cookies(_request_cookies())


def _try_localstorage_bootstrap_once() -> None:
    """Copy localStorage session id into HTTP cookie once; never block UI rendering."""
    if is_logged_in() or st.session_state.get(LS_BOOTSTRAP_FLAG) or st.session_state.get(LOGOUT_FLAG):
        return
    st.session_state[LS_BOOTSTRAP_FLAG] = True
    max_age = _cookie_max_age_seconds()
    components.html(
        f"""
        <script>
        (function () {{
            const sid = window.parent.localStorage.getItem({json.dumps(COOKIE_SID)});
            if (!sid) return;
            const doc = window.parent.document;
            doc.cookie = {json.dumps(COOKIE_SID)} + "=" + encodeURIComponent(sid)
                + "; path=/; max-age={max_age}; samesite=lax";
            window.parent.location.reload();
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


def establish_session(token: str, username: str, role: str, *, persist_browser: bool = True) -> None:
    st.session_state.pop(LOGOUT_FLAG, None)
    st.session_state[SESSION_TOKEN] = token
    st.session_state[SESSION_USER] = username
    st.session_state[SESSION_ROLE] = role
    st.session_state[SESSION_PROFILE] = ensure_profile(username, role=role)
    if persist_browser:
        _persist_auth_browser(token, username, role)


def current_username() -> str:
    return str(st.session_state.get(SESSION_USER) or "")


def current_role() -> str:
    return str(st.session_state.get(SESSION_ROLE) or "user")


def is_admin() -> bool:
    return current_role() == ADMIN_ROLE


def current_profile() -> dict[str, Any]:
    prof = st.session_state.get(SESSION_PROFILE)
    if isinstance(prof, dict):
        return prof
    if is_logged_in():
        prof = ensure_profile(current_username(), role=current_role())
        st.session_state[SESSION_PROFILE] = prof
        return prof
    return {}


def login(username: str, password: str) -> tuple[bool, str]:
    rec = authenticate(username.strip(), password)
    if not rec:
        return False, "Invalid username or password."
    token = create_access_token(rec["username"], rec["role"])
    establish_session(token, rec["username"], rec["role"])
    return True, "OK"


def logout() -> None:
    sid = str(st.session_state.get(SESSION_BROWSER_SID) or "").strip()
    if sid:
        delete_session(sid)
    for key in (SESSION_TOKEN, SESSION_USER, SESSION_ROLE, SESSION_PROFILE, SESSION_BROWSER_SID):
        st.session_state.pop(key, None)
    st.session_state.pop(LS_BOOTSTRAP_FLAG, None)
    st.session_state[LOGOUT_FLAG] = True
    _clear_browser_auth_client_side()


def auth_headers() -> dict[str, str]:
    token = st.session_state.get(SESSION_TOKEN) or ""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def api_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    url = f"{api_base_url()}{path}"
    headers = {**auth_headers(), **kwargs.pop("headers", {})}
    return requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 30), **kwargs)


def cockpit_origin() -> str:
    explicit = (os.environ.get("AK07_COCKPIT_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        headers = st.context.headers
        host = (headers.get("Host") or headers.get("host") or "").strip()
        if host:
            return f"http://{host}".rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:8501"


def try_kite_resume_from_query() -> bool:
    if is_logged_in():
        return False
    resume = (st.query_params.get("resume") or "").strip()
    if not resume:
        return False
    try:
        r = requests.post(
            f"{api_base_url()}/api/auth/kite-resume",
            json={"token": resume},
            timeout=15,
        )
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    data = r.json()
    establish_session(
        str(data.get("access_token") or ""),
        str(data.get("username") or ""),
        str(data.get("role") or "user"),
    )
    return True


def create_upstox_oauth_state(*, redirect_uri: str) -> str:
    """Store a short-lived resume so Upstox redirect can restore AK07 session via `state`."""
    if not is_logged_in():
        return ""
    state = secrets.token_urlsafe(24)
    cache_manager.set_json(
        UPSTOX_STATE_KEY.format(state=state),
        {
            "username": current_username(),
            "role": current_role(),
            "token": str(st.session_state.get(SESSION_TOKEN) or ""),
            "redirect_uri": (redirect_uri or "").strip() or f"{cockpit_origin()}/",
        },
        ttl_seconds=UPSTOX_STATE_TTL,
    )
    return state


def try_resume_upstox_oauth_state() -> bool:
    """Restore AK07 session from Upstox OAuth `state` (survives cookie host mismatch)."""
    if is_logged_in() or st.session_state.get(LOGOUT_FLAG):
        return False
    state = (st.query_params.get("state") or "").strip()
    if not state:
        return False
    key = UPSTOX_STATE_KEY.format(state=state)
    payload = cache_manager.get_json(key)
    cache_manager.delete_key(key)
    if not isinstance(payload, dict):
        return False
    token = str(payload.get("token") or "").strip()
    username = str(payload.get("username") or "").strip()
    role = str(payload.get("role") or "user").strip() or "user"
    if not token or not username:
        return False
    establish_session(token, username, role, persist_browser=True)
    redirect_uri = str(payload.get("redirect_uri") or "").strip()
    if redirect_uri:
        st.session_state[UPSTOX_PENDING_REDIRECT] = redirect_uri
    return True


def capture_upstox_oauth_code_from_query() -> bool:
    """Stash Upstox ?code= before login so it survives AK07 sign-in."""
    try_resume_upstox_oauth_state()
    code = (st.query_params.get("code") or "").strip()
    if not code:
        return False
    # Avoid treating unrelated query params as Upstox OAuth.
    if (st.query_params.get("kite") or st.query_params.get("request_token") or "").strip():
        return False
    st.session_state[UPSTOX_PENDING_CODE] = code
    if not st.session_state.get(UPSTOX_PENDING_REDIRECT):
        # Must match the redirect_uri registered with Upstox for this login.
        stored = (st.session_state.get("upstox_redirect_uri") or "").strip()
        st.session_state[UPSTOX_PENDING_REDIRECT] = stored or f"{cockpit_origin()}/"
    return True


def try_complete_upstox_oauth() -> bool:
    """If logged in with a pending Upstox code, exchange it and save the token."""
    if not is_logged_in():
        return False
    code = (st.session_state.get(UPSTOX_PENDING_CODE) or "").strip()
    if not code:
        code = (st.query_params.get("code") or "").strip()
    if not code:
        return False

    redirect_uri = (
        (st.session_state.get(UPSTOX_PENDING_REDIRECT) or "").strip()
        or (st.session_state.get("upstox_redirect_uri") or "").strip()
        or f"{cockpit_origin()}/"
    )
    st.session_state.pop(UPSTOX_PENDING_CODE, None)
    st.session_state.pop(UPSTOX_PENDING_REDIRECT, None)
    try:
        # Drop code from the URL so refresh does not re-use a spent code.
        params = {k: v for k, v in st.query_params.items() if k != "code"}
        st.query_params.clear()
        for k, v in params.items():
            st.query_params[k] = v
    except Exception:
        pass

    try:
        r = api_request(
            "POST",
            "/api/brokers/upstox/oauth/exchange",
            json={"code": code, "redirect_uri": redirect_uri},
            timeout=45,
        )
    except Exception as exc:
        st.session_state[UPSTOX_FLASH] = ("error", f"Upstox connect failed: {exc}")
        return True

    if r.status_code == 200:
        msg = (r.json() or {}).get("message") or "Upstox session connected."
        st.session_state[UPSTOX_FLASH] = ("ok", msg)
    else:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        st.session_state[UPSTOX_FLASH] = ("error", f"Upstox connect failed: {detail}")
    return True


def consume_upstox_oauth_flash() -> None:
    flash = st.session_state.pop(UPSTOX_FLASH, None)
    if not flash:
        return
    kind, msg = flash
    if kind == "ok":
        st.success(msg)
    else:
        st.error(msg)


def render_auth_sidebar() -> None:
    """Signed-in user + sign out — call once from nav_config when building logged-in nav."""
    if not is_logged_in():
        return
    user = current_username()
    role = current_role()
    with st.sidebar:
        st.caption(f"Signed in as **{user}** ({role})")
        if st.button("Sign out", key="ak07_sign_out", use_container_width=True):
            logout()
            st.rerun()


def render_login_page() -> None:
    capture_upstox_oauth_code_from_query()
    if is_logged_in():
        if try_complete_upstox_oauth():
            st.rerun()
        st.rerun()

    from app.ui.styles import inject_login_page_style, login_background_path

    inject_login_page_style(background_path=login_background_path())
    st.markdown("# AK07 Login")
    st.caption("Sign in to view your assigned strategies and broker settings.")
    if st.session_state.get(UPSTOX_PENDING_CODE):
        st.info(
            "Upstox authorized — sign in once to finish connecting "
            "(only needed if the browser session was lost)."
        )
    with st.form("ak07_login_form"):
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
    if submitted:
        ok, msg = login(username, password)
        if ok:
            try_complete_upstox_oauth()
            st.rerun()
        else:
            st.error(msg)


def require_login() -> None:
    """Auth gate for view pages. Login UI is owned by nav_config.render_login_page only."""
    if is_logged_in():
        return
    if _try_restore_auth():
        st.rerun()
    _try_localstorage_bootstrap_once()
    st.rerun()
