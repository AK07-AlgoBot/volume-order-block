"""Groww daily access token refresh (approval or TOTP)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import UserClaims, require_user
from app.models.schemas import GrowwTokenRefreshBody
from app.services.groww_token import refresh_and_save_for_user
from upstox_credentials_store import mask_tail, sanitize_username

router = APIRouter(prefix="/api/brokers/groww", tags=["groww"])


@router.post("/token/refresh")
async def groww_token_refresh(body: GrowwTokenRefreshBody, user: UserClaims = Depends(require_user)):
    from app.config.paths import ensure_repo_and_lib_on_path

    ensure_repo_and_lib_on_path()
    safe = sanitize_username(user.username)
    mode = (body.auth_mode or "approval").strip().lower()
    if mode not in ("approval", "totp"):
        raise HTTPException(status_code=400, detail="auth_mode must be 'approval' or 'totp'.")
    try:
        saved = refresh_and_save_for_user(safe, auth_mode=mode, totp=body.totp)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "credential_subject": safe,
        "access_token_preview": mask_tail(saved.get("access_token", "")),
        "token_expiry": saved.get("token_expiry", ""),
        "message": "Groww access token saved. Approve on Groww app if you used approval flow.",
    }
