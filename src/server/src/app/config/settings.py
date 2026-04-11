"""Application settings (env + derived paths)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from app.config.paths import server_root


class Settings(BaseModel):
    jwt_secret: str
    jwt_expire_minutes: int = 480
    bot_api_token: str = ""
    cors_origins: list[str]
    audit_log_max_bytes: int = 5_000_000
    audit_log_backup_count: int = 5
    # Kite Connect OAuth (https://kite.trade/docs/connect/v3/) — optional; used by /api/auth/kite/oauth/start
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_redirect_url: str = ""
    kite_post_login_redirect: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        secret = (os.environ.get("JWT_SECRET") or "").strip()
        if not secret:
            secret = "dev-insecure-change-me"
        cors_raw = os.environ.get(
            "DASHBOARD_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,https://ak07.in,http://ak07.in",
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
            audit_log_max_bytes=int(os.environ.get("AUDIT_LOG_MAX_BYTES", "5000000")),
            audit_log_backup_count=int(os.environ.get("AUDIT_LOG_BACKUP_COUNT", "5")),
            kite_api_key=(os.environ.get("KITE_API_KEY") or "").strip(),
            kite_api_secret=(os.environ.get("KITE_API_SECRET") or "").strip(),
            kite_redirect_url=(os.environ.get("KITE_REDIRECT_URL") or "").strip(),
            kite_post_login_redirect=(os.environ.get("KITE_POST_LOGIN_REDIRECT") or "").strip(),
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
