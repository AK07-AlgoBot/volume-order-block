"""Dashboard users — bcrypt hashes in users_auth.json (multi-user)."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from app.config.settings import get_settings
from app.constants import ADMIN_ROLE, DASHBOARD_USERNAME, USER_ROLE
from app.services.user_profiles_store import ensure_profile, write_profile
from app.utils.security import hash_password, verify_password

_lock = threading.Lock()


def _sanitize_username(username: str) -> str:
    u = (username or "").strip()
    u = re.sub(r"[^a-zA-Z0-9._-]", "", u)
    return u


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
    """Ensure AK07 admin exists; migrate legacy single-user installs."""
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        users: list[dict[str, Any]] = [
            u for u in (raw.get("users") or []) if isinstance(u, dict) and u.get("username")
        ]
        ak07: dict[str, Any] | None = None
        for u in users:
            if str(u.get("username", "")).strip() == DASHBOARD_USERNAME:
                ak07 = u
                break
        if not ak07 or not ak07.get("password_hash"):
            pwd = (os.environ.get("AK07_PASSWORD") or "").strip() or "change-me-rotate-for-production"
            ak07 = {
                "username": DASHBOARD_USERNAME,
                "password_hash": hash_password(pwd),
                "role": ADMIN_ROLE,
            }
            if ak07 not in users:
                users.append(ak07)
        if str(ak07.get("role") or "").strip().lower() != ADMIN_ROLE:
            ak07["role"] = ADMIN_ROLE
        _write_raw(path, {"users": users})
    ensure_profile(DASHBOARD_USERNAME, role=ADMIN_ROLE)


def list_users() -> list[dict[str, Any]]:
    ensure_seeded_users()
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        out: list[dict[str, Any]] = []
        for u in raw.get("users") or []:
            if not isinstance(u, dict):
                continue
            un = str(u.get("username", "")).strip()
            if not un:
                continue
            role = str(u.get("role") or USER_ROLE).strip().lower()
            if role not in (ADMIN_ROLE, USER_ROLE):
                role = USER_ROLE
            out.append({"username": un, "role": role})
        return sorted(out, key=lambda x: x["username"].lower())


def get_user_record(username: str) -> dict[str, Any] | None:
    ensure_seeded_users()
    un = _sanitize_username(username)
    if not un:
        return None
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        for u in raw.get("users") or []:
            if isinstance(u, dict) and str(u.get("username", "")).strip() == un:
                role = str(u.get("role") or USER_ROLE).strip().lower()
                if role not in (ADMIN_ROLE, USER_ROLE):
                    role = USER_ROLE
                return {"username": un, "password_hash": str(u.get("password_hash") or ""), "role": role}
    return None


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    rec = get_user_record(username)
    if not rec:
        return None
    h = rec.get("password_hash") or ""
    if not verify_password(password, str(h)):
        return None
    profile = ensure_profile(rec["username"], role=str(rec["role"]))
    return {
        "username": rec["username"],
        "role": rec["role"],
        "profile": profile,
    }


def create_user(
    username: str,
    password: str,
    *,
    role: str = USER_ROLE,
    enabled_strategies: list[str] | None = None,
    broker: str = "upstox",
    paper_trading: bool = True,
    lots: int = 1,
    strategy_lots: dict[str, int] | None = None,
    egress_ip: str = "",
) -> dict[str, Any]:
    un = _sanitize_username(username)
    if not un:
        raise ValueError("Invalid username.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    role_norm = (role or USER_ROLE).strip().lower()
    if role_norm not in (ADMIN_ROLE, USER_ROLE):
        role_norm = USER_ROLE
    ensure_seeded_users()
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        users = raw.get("users") or []
        for u in users:
            if isinstance(u, dict) and str(u.get("username", "")).strip().lower() == un.lower():
                raise ValueError(f"User {un} already exists.")
        users.append(
            {
                "username": un,
                "password_hash": hash_password(password),
                "role": role_norm,
            }
        )
        _write_raw(path, {"users": users})
    profile_data: dict[str, Any] = {
        "broker": broker,
        "paper_trading": paper_trading,
        "telegram_notifications": role_norm == ADMIN_ROLE,
        "lots": lots,
        "egress_ip": egress_ip,
    }
    if strategy_lots:
        profile_data["strategy_lots"] = strategy_lots
    if enabled_strategies is not None:
        profile_data["enabled_strategies"] = enabled_strategies
    profile = write_profile(un, profile_data)
    if role_norm == ADMIN_ROLE:
        profile = ensure_profile(un, role=ADMIN_ROLE)
    return {"username": un, "role": role_norm, "profile": profile}


def update_user_password(username: str, new_password: str) -> None:
    un = _sanitize_username(username)
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        found = False
        for u in raw.get("users") or []:
            if isinstance(u, dict) and str(u.get("username", "")).strip() == un:
                u["password_hash"] = hash_password(new_password)
                found = True
                break
        if not found:
            raise ValueError(f"User {un} not found.")
        _write_raw(path, raw)
