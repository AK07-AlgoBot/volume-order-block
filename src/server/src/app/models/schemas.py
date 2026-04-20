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
    manual_execution: bool | None = None
    entry_price_overridden: bool | None = None


class ManualEntryUpdateBody(BaseModel):
    trade_id: str = Field(..., min_length=1, max_length=256)
    entry_price: float = Field(..., gt=0)


class ManualTradeRemoveBody(BaseModel):
    trade_id: str = Field(..., min_length=1, max_length=256)


class ManualClosedTradeUpdateBody(BaseModel):
    trade_id: str = Field(..., min_length=1, max_length=256)
    entry_price: float | None = Field(default=None, gt=0)
    exit_price: float | None = Field(default=None, gt=0)


class QueueBotExitBody(BaseModel):
    """Ask the live trading bot to exit this open position (matched by dashboard trade id)."""

    trade_id: str = Field(..., min_length=1, max_length=512)


class WeeklyPnlPoint(BaseModel):
    date: str
    pnl: float


class UpstoxSettingsBody(BaseModel):
    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = ""


class BrokerCredentialsBody(BaseModel):
    broker: Literal["upstox", "zerodha"] = "upstox"
    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = ""


class TradingScriptsBody(BaseModel):
    """enabled_scripts=null means trade all configured symbols; otherwise a non-empty subset."""

    enabled_scripts: list[str] | None = None


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


class OrderBlockBody(BaseModel):
    """Equity / symbol for Kite order-block style analysis (e.g. RELIANCE or NSE:RELIANCE-EQ)."""

    symbol: str = Field(..., min_length=1, max_length=64)
