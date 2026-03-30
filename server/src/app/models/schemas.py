from typing import Literal

from pydantic import BaseModel, Field


class Trade(BaseModel):
    id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: float
    entry_price: float | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    chart_percent: float | None = None
    win_percent: float | None = None
    exit_price: float | None = None
    last_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    opened_at: str
    closed_at: str | None = None


class WeeklyPnlPoint(BaseModel):
    date: str
    pnl: float


class UpstoxSettingsBody(BaseModel):
    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = ""
    # Admin only: save credentials for another dashboard user
    for_user: str | None = None


class TradingScriptsBody(BaseModel):
    """enabled_scripts=null means trade all configured symbols; otherwise a non-empty subset."""

    enabled_scripts: list[str] | None = None
    for_user: str | None = None


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str


class UserPublic(BaseModel):
    username: str
    role: str
