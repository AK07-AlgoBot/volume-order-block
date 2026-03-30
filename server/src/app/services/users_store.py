"""User records with bcrypt password hashes (JSON on disk)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.config.settings import get_settings
from app.utils.security import hash_password, verify_password

_lock = threading.Lock()

DEMO_USERS: list[tuple[str, str, str]] = [
    ("AK07", "admin", "admin"),
    ("user-1", "user-1", "user"),
    ("user-2", "user-2", "user"),
    ("user-3", "user-3", "user"),
    ("user-4", "user-4", "user"),
    ("user-5", "user-5", "user"),
]


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
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        if raw.get("users"):
            return
        users = []
        for username, password, role in DEMO_USERS:
            users.append(
                {
                    "username": username,
                    "password_hash": hash_password(password),
                    "role": role,
                }
            )
        _write_raw(path, {"users": users})


def list_usernames() -> list[str]:
    ensure_seeded_users()
    path = get_settings().users_auth_path()
    with _lock:
        raw = _read_raw(path)
        return [str(u["username"]) for u in raw["users"] if isinstance(u, dict) and u.get("username")]


def get_user_record(username: str) -> dict[str, Any] | None:
    ensure_seeded_users()
    path = get_settings().users_auth_path()
    un = (username or "").strip()
    with _lock:
        raw = _read_raw(path)
        for u in raw["users"]:
            if not isinstance(u, dict):
                continue
            if str(u.get("username", "")) == un:
                return u
    return None


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    rec = get_user_record(username)
    if not rec:
        return None
    h = rec.get("password_hash") or ""
    if not verify_password(password, str(h)):
        return None
    return {"username": rec["username"], "role": str(rec.get("role", "user"))}
