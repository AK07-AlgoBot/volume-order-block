"""Application settings (env + derived paths)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from app.config.paths import server_root


DEFAULT_PRODUCTION_DOMAIN = "ak07.in"


class Settings(BaseModel):
    jwt_secret: str
    jwt_expire_minutes: int = 480
    bot_api_token: str = ""
    production_domain: str = DEFAULT_PRODUCTION_DOMAIN
    cors_origins: list[str]
    audit_log_max_bytes: int = 5_000_000
    audit_log_backup_count: int = 5

    @classmethod
    def from_env(cls) -> "Settings":
        secret = (os.environ.get("JWT_SECRET") or "").strip()
        if not secret:
            secret = "dev-insecure-change-me"
        # Production domain for operational endpoints/CORS; defaults to ak07.in
        # when PRODUCTION_DOMAIN is absent or empty in the .env.
        domain = (os.environ.get("PRODUCTION_DOMAIN") or "").strip() or DEFAULT_PRODUCTION_DOMAIN
        cors_raw = os.environ.get(
            "DASHBOARD_CORS_ORIGINS",
            "http://localhost:8501,http://127.0.0.1:8501",
        )
        origins = [o.strip() for o in cors_raw.split(",") if o.strip()] or [
            "http://localhost:8501",
            "http://127.0.0.1:8501",
        ]
        for scheme in ("https", "http"):
            origin = f"{scheme}://{domain}"
            if origin not in origins:
                origins.append(origin)
        return cls(
            jwt_secret=secret,
            production_domain=domain,
            jwt_expire_minutes=int(os.environ.get("JWT_EXPIRE_MINUTES", "480")),
            bot_api_token=(os.environ.get("BOT_API_TOKEN") or "").strip(),
            cors_origins=origins,
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
