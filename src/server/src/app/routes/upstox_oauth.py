"""Upstox OAuth — exchange authorization code for access token."""

from __future__ import annotations

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import UserClaims, require_user
from app.services.audit_log import log_action

router = APIRouter(prefix="/api/brokers/upstox", tags=["upstox"])

TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


class UpstoxOAuthExchangeBody(BaseModel):
    code: str = Field(..., min_length=4, max_length=256)
    redirect_uri: str = Field(..., min_length=8, max_length=512)


@router.post("/oauth/exchange")
async def upstox_oauth_exchange(
    body: UpstoxOAuthExchangeBody,
    user: UserClaims = Depends(require_user),
):
    """Exchange Upstox ?code= for access_token and persist for the logged-in user."""
    from upstox_credentials_store import (
        normalize_access_token,
        persist_credentials_for_user,
        read_credentials_file_for_user,
        sanitize_username,
    )

    safe = sanitize_username(user.username)
    creds = read_credentials_file_for_user(safe)
    client_id = (creds.get("api_key") or "").strip()
    client_secret = (creds.get("api_secret") or "").strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="Save Upstox API key first.")
    if not client_secret:
        raise HTTPException(status_code=400, detail="Save Upstox API secret first.")

    redirect_uri = body.redirect_uri.strip()
    code = body.code.strip()
    try:
        r = requests.post(
            TOKEN_URL,
            headers={
                "accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Upstox token request failed: {exc}") from exc

    try:
        payload = r.json()
    except Exception:
        payload = {}

    if r.status_code != 200:
        detail = (
            payload.get("error")
            or payload.get("message")
            or payload.get("errors")
            or r.text
            or f"HTTP {r.status_code}"
        )
        raise HTTPException(status_code=400, detail=f"Upstox token exchange failed: {detail}")

    access_token = normalize_access_token(str(payload.get("access_token") or ""))
    if not access_token:
        raise HTTPException(status_code=400, detail="Upstox response missing access_token.")

    merged = {
        **creds,
        "api_key": client_id,
        "api_secret": client_secret,
        "access_token": access_token,
        "base_url": creds.get("base_url") or "https://api.upstox.com/v2",
    }
    persist_credentials_for_user(safe, merged)
    log_action(safe, "upstox_oauth_exchange", {"ok": True})
    return {
        "ok": True,
        "message": "Upstox session connected — access token saved.",
        "user_id": payload.get("user_id"),
        "user_name": payload.get("user_name"),
    }
