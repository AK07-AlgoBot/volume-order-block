"""Short-lived pending records for Kite Connect OAuth (binds callback to dashboard user)."""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from app.config.paths import server_root

_PENDING_TTL_SEC = 15 * 60
_DIRNAME = "kite_oauth_pending"


def _pending_dir() -> Path:
    d = server_root() / "data" / _DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_pending(username: str) -> str:
    tid = secrets.token_urlsafe(32)
    path = _pending_dir() / f"{tid}.json"
    payload = {"username": (username or "").strip(), "exp": time.time() + _PENDING_TTL_SEC}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return tid


def consume_pending(tid: str) -> str | None:
    if not tid or any(c in tid for c in "/\\:\0"):
        return None
    path = _pending_dir() / f"{tid}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    try:
        path.unlink()
    except OSError:
        pass
    exp = float(raw.get("exp") or 0)
    if time.time() > exp:
        return None
    u = str(raw.get("username") or "").strip()
    return u or None
