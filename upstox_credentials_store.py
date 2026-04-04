"""
Per-user Upstox credentials: server/data/users/<username>/upstox_credentials.json

Single source of truth on disk — no repo-root credential file and no env-based overrides.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_BASE_URL = "https://api.upstox.com/v2"
REPO_ROOT = Path(__file__).resolve().parent


def normalize_access_token(token: str) -> str:
    t = str(token or "").strip().strip("\ufeff")
    t = t.strip().strip('"').strip("'")
    t = "".join(t.split())
    if len(t) >= 7 and t[:7].lower() == "bearer ":
        t = t[7:]
    return t.strip()


def sanitize_username(username: str) -> str:
    u = (username or "").strip()
    u = re.sub(r"[^a-zA-Z0-9._-]", "", u)
    return u or "AK07"


def user_data_dir(username: str) -> Path:
    return REPO_ROOT / "server" / "data" / "users" / sanitize_username(username)


def user_archive_order_logs(username: str) -> list[Path]:
    """users/<user>/archive/<timestamp>/logs/orders.log"""
    ud = user_data_dir(username)
    ar = ud / "archive"
    if not ar.exists():
        return []
    return sorted(ar.glob("*/logs/orders.log"))


def legacy_admin_bucket_order_logs(username: str) -> list[Path]:
    """Transitional: users/<holder>/archive/<username>/<timestamp>/logs/orders.log (holder was admin, now AK07)."""
    u = sanitize_username(username)
    seen: set[str] = set()
    out: list[Path] = []
    for holder in ("AK07", "admin"):
        base = REPO_ROOT / "server" / "data" / "users" / holder / "archive" / u
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*/logs/orders.log")):
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out


def credentials_file_for_user(username: str) -> Path:
    return user_data_dir(username) / "upstox_credentials.json"


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
    """On-disk values for this user only."""
    path = credentials_file_for_user(sanitize_username(username))
    return _read_file_raw(path)


def load_upstox_credentials_for_user(username: str) -> dict[str, str]:
    """Credentials used by the bot and API (file only)."""
    return read_credentials_file_for_user(username)


def mask_tail(value: str, tail: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= tail:
        return "•••"
    return "•••" + value[-tail:]


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


def list_usernames_from_auth_store() -> list[str]:
    """Single-tenant dashboard: only AK07."""
    return ["AK07"]
