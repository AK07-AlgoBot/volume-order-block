"""Single user (AK07) with bcrypt password hash (JSON on disk)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from app.config.settings import get_settings
from app.constants import DASHBOARD_USERNAME
from app.utils.security import hash_password, verify_password

_lock = threading.Lock()


def _read_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"users": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"users": []}
    if not isinstance(data, dict):
        return {"users": []}
    users = data.get("users")
    if not isinstance(users, list):
        data["users"] = []
    return data


def _write_raw(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def ensure_seeded_users() -> None:
    """Ensure exactly one row: AK07. Migrates away any other dashboard users."""
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        users = raw.get("users") or []
        if (
            len(users) == 1
            and isinstance(users[0], dict)
            and str(users[0].get("username", "")).strip() == DASHBOARD_USERNAME
            and users[0].get("password_hash")
        ):
            return
        ak07: dict[str, Any] | None = None
        for u in users:
            if isinstance(u, dict) and str(u.get("username", "")).strip() == DASHBOARD_USERNAME:
                ak07 = {
                    "username": DASHBOARD_USERNAME,
                    "password_hash": str(u.get("password_hash", "")),
                    "role": "user",
                }
                break
        if not ak07 or not ak07.get("password_hash"):
            pwd = (os.environ.get("AK07_PASSWORD") or "").strip() or "change-me-rotate-for-production"
            ak07 = {
                "username": DASHBOARD_USERNAME,
                "password_hash": hash_password(pwd),
                "role": "user",
            }
        _write_raw(path, {"users": [ak07]})


def get_user_record(username: str) -> dict[str, Any] | None:
    ensure_seeded_users()
    un = (username or "").strip()
    if un != DASHBOARD_USERNAME:
        return None
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        for u in raw["users"]:
            if isinstance(u, dict) and str(u.get("username", "")) == DASHBOARD_USERNAME:
                return u
    return None


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    if (username or "").strip() != DASHBOARD_USERNAME:
        return None
    rec = get_user_record(DASHBOARD_USERNAME)
    if not rec:
        return None
    h = rec.get("password_hash") or ""
    if not verify_password(password, str(h)):
        return None
    return {"username": DASHBOARD_USERNAME, "role": "user"}
