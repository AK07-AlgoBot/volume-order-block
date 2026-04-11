"""Load settings from environment — secrets never hard-coded."""

from functools import lru_cache

from pydantic import AnyHttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kite_api_key: str
    kite_api_secret: str
    kite_redirect_url: AnyHttpUrl

    session_secret: str
    host: str = "127.0.0.1"
    port: int = 8080

    @field_validator("kite_api_key", "kite_api_secret", "session_secret")
    @classmethod
    def strip_nonempty(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("must not be empty")
        return s


@lru_cache
def get_settings() -> Settings:
    return Settings()


def kite_login_url(api_key: str) -> str:
    """Official Kite Connect login URL (v3)."""
    return f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
