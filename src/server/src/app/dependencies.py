"""FastAPI dependencies: auth and bot headers."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config.paths import repo_root
from app.config.settings import get_settings
from app.constants import DASHBOARD_USERNAME
from app.services.trade_context import TradeUserContext, get_trade_context, _safe_username
from app.utils.security import decode_token


@dataclass
class UserClaims:
    username: str
    role: str


def _claims_from_authorization(request: Request) -> UserClaims | None:
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    payload = decode_token(token)
    if not payload:
        return None
    sub = str(payload.get("sub") or "").strip()
    if sub != DASHBOARD_USERNAME:
        return None
    return UserClaims(username=sub, role=str(payload.get("role", "user")))


async def require_user(request: Request) -> UserClaims:
    c = _claims_from_authorization(request)
    if not c:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization Bearer token.",
        )
    return c


def trade_context_for(claims: UserClaims) -> TradeUserContext:
    return get_trade_context(repo_root(), claims.username)


def verify_bot_trading_user(request: Request) -> str:
    """Identify target user for trading-bot HTTP posts (always AK07)."""
    s = get_settings()
    token = (request.headers.get("X-Bot-Token") or "").strip()
    header_user = (request.headers.get("X-Trading-User") or "").strip()
    if header_user and _safe_username(header_user) != DASHBOARD_USERNAME:
        raise HTTPException(
            status_code=400,
            detail=f"Only {DASHBOARD_USERNAME} is supported as X-Trading-User.",
        )

    if s.bot_api_token:
        if token != s.bot_api_token:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Bot-Token.")
        return DASHBOARD_USERNAME

    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return DASHBOARD_USERNAME
    raise HTTPException(
        status_code=401,
        detail="Set BOT_API_TOKEN on the server and send it as X-Bot-Token from remote hosts.",
    )


def trade_context_for_bot(request: Request) -> TradeUserContext:
    user = verify_bot_trading_user(request)
    return get_trade_context(repo_root(), user)


def claims_from_token_query(token: str | None) -> UserClaims:
    if not token or not token.strip():
        raise HTTPException(status_code=401, detail="WebSocket token required.")
    payload = decode_token(token.strip())
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid WebSocket token.")
    sub = str(payload.get("sub") or "").strip()
    if sub != DASHBOARD_USERNAME:
        raise HTTPException(status_code=401, detail="Invalid token subject.")
    return UserClaims(username=sub, role=str(payload.get("role", "user")))


def trade_context_for_ws(claims: UserClaims) -> TradeUserContext:
    return trade_context_for(claims)
