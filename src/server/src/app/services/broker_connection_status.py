"""Live broker connection status for the admin dashboard."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from app.config.paths import ensure_repo_and_lib_on_path
from app.services.breakout_engine import IST
from app.services.user_profiles_store import read_profile

ensure_repo_and_lib_on_path()

from broker_http import resolve_egress_ip, session_for_user  # noqa: E402
from groww_credentials_store import (  # noqa: E402
    credentials_file_for_user as groww_credentials_path,
    groww_auth_header,
    read_credentials_file_for_user as read_groww_credentials,
)
from kite_credentials_store import (  # noqa: E402
    credentials_file_for_user as kite_credentials_path,
    kite_auth_header,
    read_credentials_file_for_user as read_kite_credentials,
)
from upstox_credentials_store import (  # noqa: E402
    credentials_file_for_user as upstox_credentials_path,
    read_credentials_file_for_user as read_upstox_credentials,
)

CredentialReader = Callable[[str], dict[str, str]]
CredentialPath = Callable[[str], Path]


def _updated_today(path: Path) -> bool:
    if not path.exists():
        return False
    return datetime.fromtimestamp(path.stat().st_mtime, tz=IST).date() == datetime.now(IST).date()


def _result(
    *,
    username: str,
    broker: str,
    connected: bool,
    updated_today: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "username": username,
        "broker": broker,
        "connected": connected,
        "updated_today": updated_today,
        "status": "Connected" if connected else "Not connected",
        "detail": detail,
        "egress_ip": resolve_egress_ip(username) or "primary",
    }


def broker_connection_status(username: str, role: str = "user") -> dict[str, Any]:
    """Validate the user's selected broker token through that user's egress."""
    profile = read_profile(username, role=role)
    broker = str(profile.get("broker") or "upstox").strip().lower()
    readers: dict[str, tuple[CredentialReader, CredentialPath]] = {
        "upstox": (read_upstox_credentials, upstox_credentials_path),
        "kite": (read_kite_credentials, kite_credentials_path),
        "groww": (read_groww_credentials, groww_credentials_path),
    }
    selected = readers.get(broker)
    if selected is None:
        return _result(
            username=username,
            broker=broker,
            connected=False,
            updated_today=False,
            detail="Unsupported broker",
        )

    read_credentials, credentials_path = selected
    creds = read_credentials(username)
    updated_today = _updated_today(credentials_path(username))
    token = str(creds.get("access_token") or "").strip()
    if not token:
        return _result(
            username=username,
            broker=broker,
            connected=False,
            updated_today=updated_today,
            detail="No token saved",
        )

    session = session_for_user(username)
    try:
        if broker == "upstox":
            base = str(creds.get("base_url") or "https://api.upstox.com/v2").rstrip("/")
            response = session.get(
                f"{base}/user/profile",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=12,
            )
        elif broker == "kite":
            if not str(creds.get("api_key") or "").strip():
                return _result(
                    username=username,
                    broker=broker,
                    connected=False,
                    updated_today=updated_today,
                    detail="API key missing",
                )
            base = str(creds.get("base_url") or "https://api.kite.trade").rstrip("/")
            response = session.get(
                f"{base}/user/profile",
                headers=kite_auth_header(creds),
                timeout=12,
            )
        else:
            base = str(creds.get("base_url") or "https://api.groww.in").rstrip("/")
            response = session.get(
                f"{base}/v1/user/detail",
                headers=groww_auth_header(creds),
                timeout=12,
            )
    except requests.RequestException as exc:
        return _result(
            username=username,
            broker=broker,
            connected=False,
            updated_today=updated_today,
            detail=f"Connection error: {exc}",
        )

    connected = response.status_code == 200
    detail = "Token valid" if connected else f"HTTP {response.status_code}"
    return _result(
        username=username,
        broker=broker,
        connected=connected,
        updated_today=updated_today,
        detail=detail,
    )
