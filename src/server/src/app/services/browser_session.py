"""Server-side browser sessions — small cookie id maps to JWT in Redis."""

from __future__ import annotations

import secrets
from typing import Any

from app.services import cache_manager

SESSION_KEY_TEMPLATE = "ak07:browser_session:{sid}"


def create_session(username: str, role: str, token: str, *, ttl_seconds: int) -> str:
    sid = secrets.token_urlsafe(32)
    cache_manager.set_json(
        SESSION_KEY_TEMPLATE.format(sid=sid),
        {"username": username, "role": role, "token": token},
        ttl_seconds=ttl_seconds,
    )
    return sid


def load_session(sid: str) -> dict[str, Any] | None:
    key = SESSION_KEY_TEMPLATE.format(sid=(sid or "").strip())
    payload = cache_manager.get_json(key)
    if not isinstance(payload, dict):
        return None
    username = str(payload.get("username") or "").strip()
    token = str(payload.get("token") or "").strip()
    if not username or not token:
        return None
    role = str(payload.get("role") or "user").strip() or "user"
    return {"username": username, "role": role, "token": token}


def delete_session(sid: str) -> None:
    if sid:
        cache_manager.delete_key(SESSION_KEY_TEMPLATE.format(sid=sid.strip()))
