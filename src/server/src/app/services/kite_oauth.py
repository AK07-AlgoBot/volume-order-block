"""Kite Connect OAuth helpers — request_token → access_token exchange."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from typing import Any

import requests

from app.config.settings import get_settings
from app.services import cache_manager
from app.services.audit_log import log_action

logger = logging.getLogger("ak07.kite_oauth")

KITE_LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
KITE_TOKEN_URL = "https://api.kite.trade/session/token"
OTT_TTL_SECONDS = 600
OTT_KEY_TEMPLATE = "ak07:kite_oauth_ott:{ott}"
RESUME_KEY_TEMPLATE = "ak07:kite_oauth_resume:{token}"
COOKIE_NAME = "ak07_kite_user"
COOKIE_COCKPIT = "ak07_kite_cockpit"
COOKIE_RESUME = "ak07_kite_resume"


def kite_redirect_url() -> str:
    explicit = (os.environ.get("KITE_REDIRECT_URL") or "").strip()
    if explicit:
        return explicit
    if os.environ.get("AK07_LOCAL_DEV") == "1":
        api_port = (os.environ.get("AK07_API_PORT") or "8080").strip()
        return f"http://127.0.0.1:{api_port}/api/brokers/kite/callback"
    domain = get_settings().production_domain.strip()
    if domain:
        return f"https://{domain}/api/brokers/kite/callback"
    api_port = (os.environ.get("AK07_API_PORT") or "8080").strip()
    return f"http://127.0.0.1:{api_port}/api/brokers/kite/callback"


def default_cockpit_url() -> str:
    return (os.environ.get("AK07_COCKPIT_URL") or "http://127.0.0.1:8501").rstrip("/")


def cockpit_return_url(
    *,
    success: bool = True,
    detail: str = "",
    cockpit_base: str = "",
    resume_token: str = "",
) -> str:
    base = (cockpit_base or default_cockpit_url()).rstrip("/")
    flag = "connected" if success else "error"
    qs = f"?kite={flag}"
    if detail:
        qs += f"&msg={detail[:120]}"
    if resume_token:
        qs += f"&resume={resume_token}"
    return f"{base}/Token_Update{qs}"


def create_connect_ott(username: str, *, cockpit_url: str = "") -> str:
    ott = secrets.token_urlsafe(24)
    resume_token = secrets.token_urlsafe(32)
    cockpit = (cockpit_url or default_cockpit_url()).rstrip("/")
    cache_manager.set_json(
        OTT_KEY_TEMPLATE.format(ott=ott),
        {"username": username, "cockpit_url": cockpit, "resume_token": resume_token},
        ttl_seconds=OTT_TTL_SECONDS,
    )
    cache_manager.set_json(
        RESUME_KEY_TEMPLATE.format(token=resume_token),
        {"username": username},
        ttl_seconds=OTT_TTL_SECONDS,
    )
    return ott


def consume_connect_ott(ott: str) -> dict[str, str] | None:
    key = OTT_KEY_TEMPLATE.format(ott=(ott or "").strip())
    payload = cache_manager.get_json(key)
    cache_manager.delete_key(key)
    if not isinstance(payload, dict):
        return None
    username = str(payload.get("username") or "").strip()
    if not username:
        return None
    return {
        "username": username,
        "cockpit_url": str(payload.get("cockpit_url") or default_cockpit_url()).rstrip("/"),
        "resume_token": str(payload.get("resume_token") or "").strip(),
    }


def consume_resume_token(token: str) -> str | None:
    key = RESUME_KEY_TEMPLATE.format(token=(token or "").strip())
    payload = cache_manager.get_json(key)
    cache_manager.delete_key(key)
    if not isinstance(payload, dict):
        return None
    username = str(payload.get("username") or "").strip()
    return username or None


def exchange_request_token(
    api_key: str,
    api_secret: str,
    request_token: str,
    *,
    username: str = "",
) -> dict[str, Any]:
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    from app.config.paths import ensure_repo_and_lib_on_path

    ensure_repo_and_lib_on_path()
    from broker_http import post_for_user, session_for_user

    form = {
        "api_key": api_key,
        "request_token": request_token,
        "checksum": checksum,
    }
    if username:
        resp = post_for_user(username, KITE_TOKEN_URL, data=form, timeout=30)
    else:
        resp = session_for_user("").post(KITE_TOKEN_URL, data=form, timeout=30)
    payload: dict[str, Any] = {}
    try:
        payload = resp.json() if resp.text else {}
    except ValueError:
        payload = {}
    if resp.status_code != 200:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error_type") or payload)
        raise RuntimeError(message or f"Kite token exchange failed HTTP {resp.status_code}")
    session_data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(session_data, dict) or not session_data.get("access_token"):
        raise RuntimeError("Kite token exchange returned no access_token.")
    return session_data


def save_session_for_user(username: str, api_key: str, session: dict[str, Any]) -> dict[str, str]:
    from kite_credentials_store import persist_credentials_for_user, read_credentials_file_for_user

    current = read_credentials_file_for_user(username)
    current["api_key"] = api_key or current.get("api_key", "")
    current["access_token"] = str(session.get("access_token") or "")
    if session.get("user_id"):
        current["kite_user_id"] = str(session["user_id"])
    return persist_credentials_for_user(username, current)


def complete_oauth(username: str, request_token: str) -> dict[str, Any]:
    from kite_credentials_store import read_credentials_file_for_user

    creds = read_credentials_file_for_user(username)
    api_key = (creds.get("api_key") or "").strip()
    api_secret = (creds.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("Save Kite api_key and api_secret before connecting.")
    session = exchange_request_token(api_key, api_secret, request_token, username=username)
    saved = save_session_for_user(username, api_key, session)
    log_action(
        username,
        "kite_oauth_connected",
        {"kite_user_id": session.get("user_id"), "user_name": session.get("user_name")},
        target_user=username,
    )
    return {"session": session, "saved": saved}
