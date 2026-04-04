"""Structured audit log per user + admin-readable paths."""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config.settings import get_settings

_lock = threading.Lock()
_loggers: dict[str, logging.Logger] = {}


def _logger_for_user(username: str) -> logging.Logger:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in username) or "unknown"
    with _lock:
        if safe in _loggers:
            return _loggers[safe]
    s = get_settings()
    log_dir = s.audit_dir() / safe
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "actions.log"
    lg = logging.getLogger(f"audit.{safe}")
    lg.setLevel(logging.INFO)
    lg.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=s.audit_log_max_bytes,
        backupCount=s.audit_log_backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(fh)
    lg.propagate = False
    with _lock:
        _loggers[safe] = lg
    return lg


def log_action(
    actor_username: str,
    action: str,
    detail: dict[str, Any] | None = None,
    target_user: str | None = None,
) -> None:
    """Append one JSON line (actor may be admin viewing as another user)."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor_username,
        "action": action,
        "target_user": target_user,
        "detail": detail or {},
    }
    line = json.dumps(payload, ensure_ascii=False)
    _logger_for_user(actor_username).info(line)


def read_recent_audit_lines(username: str, max_lines: int = 200) -> list[str]:
    """Return last N lines from a user's audit file (admin use)."""
    s = get_settings()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in username) or "unknown"
    log_path = s.audit_dir() / safe / "actions.log"
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]
