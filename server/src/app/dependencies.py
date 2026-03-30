"""FastAPI dependencies: auth, effective data tenant, bot headers."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config.settings import get_settings, repo_root
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
    if not sub:
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


async def require_admin_dep(request: Request) -> UserClaims:
    c = await require_user(request)
    if c.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")
    return c


def effective_data_username(claims: UserClaims, view_as: str | None) -> str:
    if claims.role == "admin" and view_as and view_as.strip():
        return _safe_username(view_as.strip())
    if view_as and view_as.strip() and claims.role != "admin":
        raise HTTPException(status_code=403, detail="Only admin may use view_as.")
    return claims.username


def trade_context_for(
    claims: UserClaims,
    view_as: str | None,
) -> TradeUserContext:
    user = effective_data_username(claims, view_as)
    return get_trade_context(repo_root(), user)


def verify_bot_trading_user(request: Request) -> str:
    """Identify target user for trading-bot HTTP posts."""
    s = get_settings()
    token = (request.headers.get("X-Bot-Token") or "").strip()
    user = _safe_username((request.headers.get("X-Trading-User") or "user-1").strip())

    if s.bot_api_token:
        if token != s.bot_api_token:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Bot-Token.")
        return user

    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return user
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
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token subject.")
    return UserClaims(username=sub, role=str(payload.get("role", "user")))


def trade_context_for_ws(claims: UserClaims, view_as: str | None) -> TradeUserContext:
    return trade_context_for(claims, view_as)
