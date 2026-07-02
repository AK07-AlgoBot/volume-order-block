"""
Per-user Kite (Zerodha) credentials: src/server/data/users/<username>/kite_credentials.json
"""

from __future__ import annotations

import json
from pathlib import Path

from upstox_credentials_store import (
    REPO_ROOT,
    mask_tail,
    normalize_access_token,
    sanitize_username,
)

DEFAULT_KITE_BASE_URL = "https://api.kite.trade"


def credentials_file_for_user(username: str) -> Path:
    return REPO_ROOT / "src" / "server" / "data" / "users" / sanitize_username(username) / "kite_credentials.json"


def _empty() -> dict[str, str]:
    return {
        "api_key": "",
        "access_token": "",
        "api_secret": "",
        "base_url": DEFAULT_KITE_BASE_URL,
    }


def read_credentials_file_for_user(username: str) -> dict[str, str]:
    path = credentials_file_for_user(username)
    base = _empty()
    if not path.exists():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(raw, dict):
        return base
    for key in base:
        if key in raw and raw[key] is not None:
            base[key] = str(raw[key]).strip() or base[key]
    if base.get("access_token"):
        base["access_token"] = normalize_access_token(base["access_token"])
    return base


def persist_credentials_for_user(username: str, data: dict[str, str]) -> dict[str, str]:
    out = {
        "api_key": str(data.get("api_key", "")).strip(),
        "access_token": normalize_access_token(str(data.get("access_token", ""))),
        "api_secret": str(data.get("api_secret", "")).strip(),
        "base_url": str(data.get("base_url") or "").strip() or DEFAULT_KITE_BASE_URL,
    }
    path = credentials_file_for_user(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def kite_auth_header(creds: dict[str, str]) -> dict[str, str]:
    api_key = (creds.get("api_key") or "").strip()
    token = (creds.get("access_token") or "").strip()
    return {"Authorization": f"token {api_key}:{token}", "Accept": "application/json"}
