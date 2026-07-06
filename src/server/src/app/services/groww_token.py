"""Groww Trade API — daily access token via approval (checksum) or TOTP."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import requests

from app.services.audit_log import log_action

logger = logging.getLogger("ak07.groww_token")

GROWW_TOKEN_PATH = "/v1/token/api/access"
GROWW_USER_DETAIL_PATH = "/v1/user/detail"


def generate_checksum(api_secret: str, timestamp: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"{api_secret}{timestamp}".encode("utf-8"))
    return digest.hexdigest()


def _token_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}{GROWW_TOKEN_PATH}"


def _user_detail_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}{GROWW_USER_DETAIL_PATH}"


def _parse_token_response(payload: dict[str, Any]) -> tuple[str, str]:
    """Return (access_token, expiry_iso)."""
    if not isinstance(payload, dict):
        raise RuntimeError("Groww token response was not JSON.")
    data = payload
    if payload.get("status") == "SUCCESS" and isinstance(payload.get("payload"), dict):
        data = payload["payload"]
    token = str(data.get("token") or data.get("access_token") or "").strip()
    if not token:
        err = payload.get("error")
        if isinstance(err, dict):
            raise RuntimeError(str(err.get("message") or err))
        raise RuntimeError(str(payload.get("message") or "Groww token exchange returned no token."))
    expiry = str(data.get("expiry") or data.get("expires_at") or "").strip()
    return token, expiry


def exchange_approval_token(*, api_key: str, api_secret: str, base_url: str) -> tuple[str, str]:
    timestamp = str(int(time.time()))
    checksum = generate_checksum(api_secret, timestamp)
    resp = requests.post(
        _token_url(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "key_type": "approval",
            "checksum": checksum,
            "timestamp": timestamp,
        },
        timeout=30,
    )
    payload: dict[str, Any] = {}
    try:
        payload = resp.json() if resp.text else {}
    except ValueError:
        payload = {}
    if resp.status_code != 200:
        message = ""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or err)
            else:
                message = str(payload.get("message") or payload)
        raise RuntimeError(message or f"Groww token exchange failed HTTP {resp.status_code}")
    return _parse_token_response(payload)


def exchange_totp_token(*, api_key: str, totp: str, base_url: str) -> tuple[str, str]:
    code = (totp or "").strip()
    if len(code) < 6:
        raise RuntimeError("Enter the 6-digit TOTP from your authenticator app.")
    resp = requests.post(
        _token_url(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "key_type": "totp",
            "totp": code,
        },
        timeout=30,
    )
    payload: dict[str, Any] = {}
    try:
        payload = resp.json() if resp.text else {}
    except ValueError:
        payload = {}
    if resp.status_code != 200:
        message = ""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or err)
            else:
                message = str(payload.get("message") or payload)
        raise RuntimeError(message or f"Groww TOTP exchange failed HTTP {resp.status_code}")
    return _parse_token_response(payload)


def fetch_user_profile(*, access_token: str, base_url: str) -> dict[str, Any]:
    resp = requests.get(
        _user_detail_url(base_url),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "X-API-VERSION": "1.0",
        },
        timeout=30,
    )
    payload: dict[str, Any] = {}
    try:
        payload = resp.json() if resp.text else {}
    except ValueError:
        payload = {}
    if resp.status_code != 200:
        message = ""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or err)
            else:
                message = str(payload.get("message") or payload)
        raise RuntimeError(message or f"Groww profile check failed HTTP {resp.status_code}")
    if isinstance(payload, dict) and payload.get("status") == "FAILURE":
        err = payload.get("error")
        if isinstance(err, dict):
            raise RuntimeError(str(err.get("message") or err))
        raise RuntimeError("Groww profile check failed.")
    data = payload.get("payload") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return data
    return payload if isinstance(payload, dict) else {}


def refresh_and_save_for_user(
    username: str,
    *,
    auth_mode: str = "approval",
    totp: str = "",
) -> dict[str, str]:
    from groww_credentials_store import persist_credentials_for_user, read_credentials_file_for_user

    creds = read_credentials_file_for_user(username)
    api_key = (creds.get("api_key") or "").strip()
    api_secret = (creds.get("api_secret") or "").strip()
    base_url = (creds.get("base_url") or "").strip()
    if not api_key:
        raise RuntimeError("Save Groww api_key first.")
    mode = (auth_mode or "approval").strip().lower()
    if mode == "totp":
        token, expiry = exchange_totp_token(api_key=api_key, totp=totp, base_url=base_url)
    else:
        if not api_secret:
            raise RuntimeError("Save Groww api_secret first (approval flow).")
        token, expiry = exchange_approval_token(api_key=api_key, api_secret=api_secret, base_url=base_url)
    saved = persist_credentials_for_user(
        username,
        {**creds, "access_token": token, "token_expiry": expiry},
    )
    log_action(
        username,
        "groww_token_refreshed",
        {"auth_mode": mode, "token_expiry": expiry},
        target_user=username,
    )
    return saved
