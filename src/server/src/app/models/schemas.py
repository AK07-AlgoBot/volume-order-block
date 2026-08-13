from typing import Literal

from pydantic import BaseModel, Field


class UpstoxSettingsBody(BaseModel):
    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = ""


class BrokerCredentialsBody(BaseModel):
    broker: Literal["upstox", "kite", "groww"] = "upstox"
    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = ""
    redirect_uri: str = ""


class GrowwTokenRefreshBody(BaseModel):
    auth_mode: Literal["approval", "totp"] = "approval"
    totp: str = ""


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class KiteConnectStartBody(BaseModel):
    cockpit_url: str = ""


class KiteResumeBody(BaseModel):
    token: str = Field(..., min_length=8, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str


class UserPublic(BaseModel):
    username: str
    role: str


class UserProfilePublic(BaseModel):
    username: str
    role: str
    enabled_strategies: list[str]
    broker: str
    paper_trading: bool
    lots: int = 1
    egress_ip: str = ""


class CreateUserBody(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=8, max_length=256)
    role: Literal["admin", "user"] = "user"
    enabled_strategies: list[str] = Field(default_factory=list)
    broker: Literal["upstox", "kite", "groww"] = "upstox"
    paper_trading: bool = True
    lots: int = Field(default=1, ge=1, le=20)
    egress_ip: str = ""


class UpdateUserProfileBody(BaseModel):
    enabled_strategies: list[str] | None = None
    broker: Literal["upstox", "kite", "groww"] | None = None
    paper_trading: bool | None = None
    lots: int | None = Field(default=None, ge=1, le=20)
    egress_ip: str | None = None


class AdminBlrUpdateBody(BaseModel):
    index_code: Literal["NIFTY", "BANKNIFTY", "SENSEX"] = "NIFTY"
    green: float = Field(..., gt=0)
    mid: float = Field(..., gt=0)
    red: float = Field(..., gt=0)


class ResetPasswordBody(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=256)


class UpstoxTokenNotifierBody(BaseModel):
    client_id: str = ""
    user_id: str = ""
    access_token: str = ""
    token_type: str = ""
    expires_at: str = ""
    issued_at: str = ""
    message_type: str = ""

