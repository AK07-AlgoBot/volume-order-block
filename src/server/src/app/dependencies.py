"""FastAPI dependencies: AK07 auth only."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.constants import DASHBOARD_USERNAME
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


