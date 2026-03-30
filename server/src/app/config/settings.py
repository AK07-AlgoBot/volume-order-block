"""Application settings (env + derived paths)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


def repo_root() -> Path:
    """Repository root (parent of server/)."""
    return Path(__file__).resolve().parents[4]


def server_root() -> Path:
    return Path(__file__).resolve().parents[3]


class Settings(BaseModel):
    jwt_secret: str
    jwt_expire_minutes: int = 480
    bot_api_token: str = ""
    cors_origins: list[str]
    dashboard_admin_token: str = ""
    audit_log_max_bytes: int = 5_000_000
    audit_log_backup_count: int = 5

    @classmethod
    def from_env(cls) -> "Settings":
        secret = (os.environ.get("JWT_SECRET") or "").strip()
        if not secret:
            secret = "dev-insecure-change-me"
        cors_raw = os.environ.get(
            "DASHBOARD_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        )
        origins = [o.strip() for o in cors_raw.split(",") if o.strip()] or [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
        return cls(
            jwt_secret=secret,
            jwt_expire_minutes=int(os.environ.get("JWT_EXPIRE_MINUTES", "480")),
            bot_api_token=(os.environ.get("BOT_API_TOKEN") or "").strip(),
            cors_origins=origins,
            dashboard_admin_token=(os.environ.get("DASHBOARD_ADMIN_TOKEN") or "").strip(),
            audit_log_max_bytes=int(os.environ.get("AUDIT_LOG_MAX_BYTES", "5000000")),
            audit_log_backup_count=int(os.environ.get("AUDIT_LOG_BACKUP_COUNT", "5")),
        )

    def user_data_dir(self, username: str) -> Path:
        safe = _sanitize_username(username)
        return server_root() / "data" / "users" / safe

    def users_auth_path(self) -> Path:
        return server_root() / "data" / "users_auth.json"

    def audit_dir(self) -> Path:
        return server_root() / "data" / "logs" / "audit"


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


def _sanitize_username(username: str) -> str:
    u = (username or "").strip()
    if not u or any(c in u for c in "/\\:\0"):
        return "invalid"
    return u
