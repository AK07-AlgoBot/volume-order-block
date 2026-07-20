"""Per-user dashboard profile: strategy entitlements, broker preference, flags."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from app.config.settings import get_settings
from app.constants import (
    ADMIN_ROLE,
    ALL_STRATEGIES,
    DASHBOARD_USERNAME,
    SUPPORTED_BROKERS,
    USER_ROLE,
)

_lock = threading.Lock()


def _sanitize_username(username: str) -> str:
    u = (username or "").strip()
    u = re.sub(r"[^a-zA-Z0-9._-]", "", u)
    return u


def profile_path(username: str) -> Path:
    safe = _sanitize_username(username)
    return get_settings().user_data_dir(safe) / "profile.json"


def _default_profile(username: str, role: str = USER_ROLE) -> dict[str, Any]:
    enabled = list(ALL_STRATEGIES) if role == ADMIN_ROLE else [ALL_STRATEGIES[2]]  # S3 default
    return {
        "username": _sanitize_username(username),
        "role": role,
        "enabled_strategies": enabled,
        "broker": "upstox",
        "paper_trading": True,
        "telegram_notifications": role == ADMIN_ROLE,
        "egress_ip": "",
    }


def read_profile(username: str, *, role: str = USER_ROLE) -> dict[str, Any]:
    safe = _sanitize_username(username)
    path = profile_path(safe)
    base = _default_profile(safe, role=role)
    if not path.exists():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(raw, dict):
        return base
    strategies = raw.get("enabled_strategies")
    if isinstance(strategies, list):
        base["enabled_strategies"] = [s for s in strategies if s in ALL_STRATEGIES]
    broker = str(raw.get("broker") or "upstox").strip().lower()
    base["broker"] = broker if broker in SUPPORTED_BROKERS else "upstox"
    base["paper_trading"] = bool(raw.get("paper_trading", base["paper_trading"]))
    if "telegram_notifications" in raw:
        base["telegram_notifications"] = bool(raw.get("telegram_notifications"))
    egress_ip = str(raw.get("egress_ip") or "").strip()
    base["egress_ip"] = egress_ip
    return base


def write_profile(username: str, data: dict[str, Any]) -> dict[str, Any]:
    safe = _sanitize_username(username)
    current = read_profile(safe)
    if "enabled_strategies" in data and isinstance(data["enabled_strategies"], list):
        current["enabled_strategies"] = [s for s in data["enabled_strategies"] if s in ALL_STRATEGIES]
    if "broker" in data:
        broker = str(data["broker"] or "upstox").strip().lower()
        current["broker"] = broker if broker in SUPPORTED_BROKERS else current["broker"]
    if "paper_trading" in data:
        current["paper_trading"] = bool(data["paper_trading"])
    if "telegram_notifications" in data:
        current["telegram_notifications"] = bool(data["telegram_notifications"])
    if "egress_ip" in data:
        current["egress_ip"] = str(data.get("egress_ip") or "").strip()
    path = profile_path(safe)
    path.parent.mkdir(parents=True, exist_ok=True)
    current["username"] = safe
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return current


def ensure_profile(username: str, *, role: str = USER_ROLE) -> dict[str, Any]:
    safe = _sanitize_username(username)
    with _lock:
        path = profile_path(safe)
        if path.exists():
            prof = read_profile(safe, role=role)
            if role == ADMIN_ROLE and set(prof.get("enabled_strategies") or []) != set(ALL_STRATEGIES):
                prof["enabled_strategies"] = list(ALL_STRATEGIES)
                write_profile(safe, prof)
            return prof
        prof = _default_profile(safe, role=role)
        if safe == DASHBOARD_USERNAME and role == ADMIN_ROLE:
            prof["enabled_strategies"] = list(ALL_STRATEGIES)
            prof["paper_trading"] = False
            prof["telegram_notifications"] = True
        write_profile(safe, prof)
        return prof


def strategy_enabled(profile: dict[str, Any], strategy_id: str, *, role: str | None = None) -> bool:
    if role == ADMIN_ROLE or profile.get("role") == ADMIN_ROLE:
        return True
    return strategy_id in (profile.get("enabled_strategies") or [])


def telegram_notifications_enabled(profile: dict[str, Any], *, role: str | None = None) -> bool:
    """Telegram alerts — admin-only until per-user fan-out is enabled."""
    is_admin = role == ADMIN_ROLE or profile.get("role") == ADMIN_ROLE
    default = True if is_admin else False
    return bool(profile.get("telegram_notifications", default))
