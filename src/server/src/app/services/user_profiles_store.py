"""Per-user dashboard profile: strategy entitlements, broker preference, flags."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config.settings import get_settings
from app.constants import (
    ADMIN_ROLE,
    ALL_STRATEGIES,
    DASHBOARD_USERNAME,
    SUPPORTED_BROKERS,
    USER_ROLE,
)

_lock = threading.Lock()
_IST = ZoneInfo("Asia/Kolkata")


def _sanitize_username(username: str) -> str:
    u = (username or "").strip()
    u = re.sub(r"[^a-zA-Z0-9._-]", "", u)
    return u


def _now_iso() -> str:
    return datetime.now(_IST).isoformat()


def normalize_lots(value: Any, *, default: int = 1) -> int:
    """S3 quantity allocation in lots (1–20)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, 20))


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
        "lots": 1,
        "created_at": _now_iso(),
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
    if "lots" in raw:
        base["lots"] = normalize_lots(raw.get("lots"))
    created_at = str(raw.get("created_at") or "").strip()
    if created_at:
        base["created_at"] = created_at
    else:
        # Backfill from profile file mtime so existing users get a stable first-day.
        try:
            base["created_at"] = datetime.fromtimestamp(path.stat().st_mtime, tz=_IST).isoformat()
        except OSError:
            pass
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
    if "lots" in data:
        current["lots"] = normalize_lots(data.get("lots"), default=int(current.get("lots") or 1))
    if "created_at" in data and str(data.get("created_at") or "").strip():
        current["created_at"] = str(data["created_at"]).strip()
    elif not str(current.get("created_at") or "").strip():
        current["created_at"] = _now_iso()
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
            dirty = False
            if role == ADMIN_ROLE and set(prof.get("enabled_strategies") or []) != set(ALL_STRATEGIES):
                prof["enabled_strategies"] = list(ALL_STRATEGIES)
                dirty = True
            if not str(prof.get("created_at") or "").strip():
                try:
                    prof["created_at"] = datetime.fromtimestamp(path.stat().st_mtime, tz=_IST).isoformat()
                except OSError:
                    prof["created_at"] = _now_iso()
                dirty = True
            if dirty:
                write_profile(safe, prof)
            return prof
        prof = _default_profile(safe, role=role)
        if safe == DASHBOARD_USERNAME and role == ADMIN_ROLE:
            prof["enabled_strategies"] = list(ALL_STRATEGIES)
            prof["paper_trading"] = False
            prof["telegram_notifications"] = True
        write_profile(safe, prof)
        return prof


def profile_created_date(username: str, *, role: str = USER_ROLE):
    """Calendar date the user was onboarded (IST)."""
    from datetime import date as date_cls

    prof = read_profile(username, role=role)
    raw = str(prof.get("created_at") or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_IST)
            return dt.astimezone(_IST).date()
        except ValueError:
            pass
    return date_cls.today()


def strategy_enabled(profile: dict[str, Any], strategy_id: str, *, role: str | None = None) -> bool:
    if strategy_id not in ALL_STRATEGIES:
        return False
    if role == ADMIN_ROLE or profile.get("role") == ADMIN_ROLE:
        return True
    return strategy_id in (profile.get("enabled_strategies") or [])


def telegram_notifications_enabled(profile: dict[str, Any], *, role: str | None = None) -> bool:
    """Telegram alerts — admin-only until per-user fan-out is enabled."""
    is_admin = role == ADMIN_ROLE or profile.get("role") == ADMIN_ROLE
    default = True if is_admin else False
    return bool(profile.get("telegram_notifications", default))
