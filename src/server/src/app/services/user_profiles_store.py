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
    STRATEGY_GC_OF,
    STRATEGY_S3_BREAKOUT,
    STRATEGY_S29_ORB,
    SUPPORTED_BROKERS,
    USER_ROLE,
)

_lock = threading.Lock()
_IST = ZoneInfo("Asia/Kolkata")
_LEGACY_STRATEGY_MAP = {"s7_orb": "s29_orb", "s7_vmb": "s29_orb"}


def _normalize_enabled_strategies(strategies: list[Any]) -> list[str]:
    out: list[str] = []
    for raw in strategies:
        sid = _LEGACY_STRATEGY_MAP.get(str(raw), str(raw))
        if sid in ALL_STRATEGIES and sid not in out:
            out.append(sid)
    if STRATEGY_GC_OF in ALL_STRATEGIES and STRATEGY_GC_OF not in out:
        if STRATEGY_S3_BREAKOUT in out or STRATEGY_S29_ORB in out:
            out.append(STRATEGY_GC_OF)
    return out


def _sanitize_username(username: str) -> str:
    u = (username or "").strip()
    u = re.sub(r"[^a-zA-Z0-9._-]", "", u)
    return u


def _now_iso() -> str:
    return datetime.now(_IST).isoformat()


def normalize_lots(value: Any, *, default: int = 1) -> int:
    """Quantity allocation in lots (1–20)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, 20))


def normalize_strategy_lots(raw: Any, *, default: int = 1) -> dict[str, int]:
    """Per-strategy lots; missing keys fall back to ``default`` (legacy global lots)."""
    out: dict[str, int] = {sid: default for sid in ALL_STRATEGIES}
    if isinstance(raw, dict):
        for key, val in raw.items():
            sid = _LEGACY_STRATEGY_MAP.get(str(key), str(key))
            if sid in ALL_STRATEGIES:
                out[sid] = normalize_lots(val, default=default)
    return out


def lots_for_strategy(profile: dict[str, Any], strategy_id: str, *, default: int = 1) -> int:
    """Lots for one strategy; falls back to the profile-wide ``lots`` field."""
    sid = _LEGACY_STRATEGY_MAP.get(str(strategy_id), str(strategy_id))
    sl = profile.get("strategy_lots")
    if isinstance(sl, dict) and sid in sl:
        return normalize_lots(sl.get(sid), default=default)
    return normalize_lots(profile.get("lots"), default=default)


def profile_path(username: str) -> Path:
    safe = _sanitize_username(username)
    return get_settings().user_data_dir(safe) / "profile.json"


def _default_profile(username: str, role: str = USER_ROLE) -> dict[str, Any]:
    enabled = list(ALL_STRATEGIES) if role == ADMIN_ROLE else [STRATEGY_S3_BREAKOUT, STRATEGY_GC_OF]
    return {
        "username": _sanitize_username(username),
        "role": role,
        "enabled_strategies": enabled,
        "broker": "upstox",
        "paper_trading": True,
        "telegram_notifications": role == ADMIN_ROLE,
        "egress_ip": "",
        "lots": 1,
        "strategy_lots": {sid: 1 for sid in ALL_STRATEGIES},
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
        base["enabled_strategies"] = _normalize_enabled_strategies(strategies)
    broker = str(raw.get("broker") or "upstox").strip().lower()
    base["broker"] = broker if broker in SUPPORTED_BROKERS else "upstox"
    base["paper_trading"] = bool(raw.get("paper_trading", base["paper_trading"]))
    if "telegram_notifications" in raw:
        base["telegram_notifications"] = bool(raw.get("telegram_notifications"))
    egress_ip = str(raw.get("egress_ip") or "").strip()
    base["egress_ip"] = egress_ip
    fallback_lots = normalize_lots(raw.get("lots"), default=1) if "lots" in raw else 1
    base["lots"] = fallback_lots
    base["strategy_lots"] = normalize_strategy_lots(raw.get("strategy_lots"), default=fallback_lots)
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
        current["enabled_strategies"] = _normalize_enabled_strategies(data["enabled_strategies"])
    if "broker" in data:
        broker = str(data["broker"] or "upstox").strip().lower()
        current["broker"] = broker if broker in SUPPORTED_BROKERS else current["broker"]
    if "paper_trading" in data:
        current["paper_trading"] = bool(data["paper_trading"])
    if "telegram_notifications" in data:
        current["telegram_notifications"] = bool(data["telegram_notifications"])
    if "egress_ip" in data:
        current["egress_ip"] = str(data.get("egress_ip") or "").strip()
    fallback_lots = int(current.get("lots") or 1)
    if "lots" in data:
        fallback_lots = normalize_lots(data.get("lots"), default=fallback_lots)
        current["lots"] = fallback_lots
    if "strategy_lots" in data:
        current["strategy_lots"] = normalize_strategy_lots(
            data.get("strategy_lots"), default=fallback_lots
        )
        current["lots"] = current["strategy_lots"].get(STRATEGY_S3_BREAKOUT, fallback_lots)
    elif "lots" in data:
        current["strategy_lots"] = normalize_strategy_lots(
            current.get("strategy_lots"), default=fallback_lots
        )
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
