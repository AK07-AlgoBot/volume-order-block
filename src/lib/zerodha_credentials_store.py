"""
Per-user Zerodha Kite Connect credentials: src/server/data/users/<username>/zerodha_credentials.json

Same shape as Upstox store for dashboard parity (base_url, access_token, api_key, api_secret).
Kite REST base URL: https://api.kite.trade
"""

from __future__ import annotations

import json
from pathlib import Path

from upstox_credentials_store import (
    normalize_access_token,
    sanitize_username,
    user_data_dir,
)

DEFAULT_BASE_URL = "https://api.kite.trade"


def credentials_file_for_user(username: str) -> Path:
    return user_data_dir(username) / "zerodha_credentials.json"


def _empty_credential_dict() -> dict[str, str]:
    return {
        "access_token": "",
        "api_key": "",
        "api_secret": "",
        "base_url": DEFAULT_BASE_URL,
    }


def _read_file_raw(path: Path) -> dict[str, str]:
    base = _empty_credential_dict()
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


def read_credentials_file_for_user(username: str) -> dict[str, str]:
    path = credentials_file_for_user(sanitize_username(username))
    return _read_file_raw(path)


def load_zerodha_credentials_for_user(username: str) -> dict[str, str]:
    return read_credentials_file_for_user(username)


def persist_credentials_for_user(username: str, data: dict[str, str]) -> dict[str, str]:
    out = {
        "access_token": normalize_access_token(str(data.get("access_token", ""))),
        "api_key": str(data.get("api_key", "")).strip(),
        "api_secret": str(data.get("api_secret", "")).strip(),
        "base_url": str(data.get("base_url") or "").strip() or DEFAULT_BASE_URL,
    }
    path = credentials_file_for_user(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out
