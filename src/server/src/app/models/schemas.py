from typing import Literal

from pydantic import BaseModel, Field


class UpstoxSettingsBody(BaseModel):
    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = ""


class BrokerCredentialsBody(BaseModel):
    broker: Literal["upstox"] = "upstox"
    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = ""


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

