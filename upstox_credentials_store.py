"""
Upstox credentials on disk (upstox_credentials.json, gitignored).

Env vars fill empty token/key/secret fields. If upstox_credentials.json exists,
on-disk values win for those fields (so a stale UPSTOX_ACCESS_TOKEN env var cannot
override a token saved from the dashboard). UPSTOX_BASE_URL applies only when the
credentials file does not exist yet.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_BASE_URL = "https://api.upstox.com/v2"
CREDENTIALS_FILE = Path(__file__).resolve().parent / "upstox_credentials.json"


def normalize_access_token(token: str) -> str:
    """
    Strip wrapping quotes, whitespace, accidental 'Bearer ' prefix, and BOM debris.
    JWTs have no spaces; removing whitespace fixes paste errors.
    """
    t = str(token or "").strip().strip("\ufeff")
    t = t.strip().strip('"').strip("'")
    t = "".join(t.split())
    if len(t) >= 7 and t[:7].lower() == "bearer ":
        t = t[7:]
    return t.strip()


def _env(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None


def read_credentials_file() -> dict[str, str]:
    """Values as stored on disk only (no env overlay)."""
    base = {
        "access_token": "",
        "api_key": "",
        "api_secret": "",
        "base_url": DEFAULT_BASE_URL,
    }
    if not CREDENTIALS_FILE.exists():
        return base
    try:
        raw = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return base
    if not isinstance(raw, dict):
        return base
    for key in base:
        if key in raw and raw[key] is not None:
            base[key] = str(raw[key]).strip() or base[key]
    if base.get("access_token"):
        base["access_token"] = normalize_access_token(base["access_token"])
    return base


def load_upstox_credentials() -> dict[str, str]:
    """File first; env vars fill only empty fields (dashboard saves must not be overridden)."""
    data = read_credentials_file()
    if not data.get("access_token", "").strip():
        if _env("UPSTOX_ACCESS_TOKEN"):
            data["access_token"] = normalize_access_token(_env("UPSTOX_ACCESS_TOKEN") or "")
    if not data.get("api_key", "").strip():
        if _env("UPSTOX_API_KEY"):
            data["api_key"] = _env("UPSTOX_API_KEY") or ""
    if not data.get("api_secret", "").strip():
        if _env("UPSTOX_API_SECRET"):
            data["api_secret"] = _env("UPSTOX_API_SECRET") or ""
    if not CREDENTIALS_FILE.exists() and _env("UPSTOX_BASE_URL"):
        data["base_url"] = _env("UPSTOX_BASE_URL") or data["base_url"]
    return data


def mask_tail(value: str, tail: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= tail:
        return "•••"
    return "•••" + value[-tail:]


def persist_credentials(data: dict[str, str]) -> dict[str, str]:
    """Write a full credential dict to disk (caller merges partial updates)."""
    out = {
        "access_token": normalize_access_token(str(data.get("access_token", ""))),
        "api_key": str(data.get("api_key", "")).strip(),
        "api_secret": str(data.get("api_secret", "")).strip(),
        "base_url": str(data.get("base_url") or "").strip() or DEFAULT_BASE_URL,
    }
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out
