import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.dependencies import UserClaims, require_user
from app.models.schemas import UpstoxSettingsBody
from app.services.audit_log import log_action
from app.services.users_store import get_user_record

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _ensure_repo_on_path() -> None:
    import sys
    from pathlib import Path

    from app.config.settings import repo_root

    r = str(repo_root())
    if r not in sys.path:
        sys.path.insert(0, r)


def _resolve_credential_subject(
    actor: UserClaims,
    for_user: str | None,
) -> str:
    target = (for_user or "").strip()
    if not target:
        return actor.username
    if actor.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin may use for_user to view or save another account's credentials.",
        )
    rec = get_user_record(target)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Unknown user: {target}")
    return target


@router.get("/upstox")
async def get_upstox_settings(
    user: UserClaims = Depends(require_user),
    for_user: str | None = Query(None, description="Admin: read another user's credential previews"),
):
    _ensure_repo_on_path()
    from upstox_credentials_store import (  # noqa: PLC0415
        credentials_file_for_user,
        mask_tail,
        read_credentials_file_for_user,
        sanitize_username,
    )

    subject = _resolve_credential_subject(user, for_user)
    safe = sanitize_username(subject)
    data = read_credentials_file_for_user(safe)
    admin_required = bool(os.environ.get("DASHBOARD_ADMIN_TOKEN", "").strip())
    cred_path = credentials_file_for_user(safe)
    return {
        "base_url": data["base_url"],
        "access_token_preview": mask_tail(data["access_token"]),
        "api_key_preview": mask_tail(data["api_key"]),
        "api_secret_preview": mask_tail(data["api_secret"]),
        "has_access_token": bool(data["access_token"]),
        "has_api_key": bool(data["api_key"]),
        "has_api_secret": bool(data["api_secret"]),
        "credentials_file": cred_path.name,
        "credentials_path": str(cred_path.resolve()),
        "credential_subject": safe,
        "admin_token_configured": admin_required,
    }


def _require_legacy_dashboard_admin_token(request: Request) -> None:
    expected = os.environ.get("DASHBOARD_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    got = (request.headers.get("X-Dashboard-Admin-Token") or "").strip()
    if got != expected:
        raise HTTPException(
            status_code=401,
            detail="Set header X-Dashboard-Admin-Token to match DASHBOARD_ADMIN_TOKEN on the server.",
        )


@router.post("/upstox")
async def post_upstox_settings(
    request: Request,
    body: UpstoxSettingsBody,
    actor: UserClaims = Depends(require_user),
):
    _require_legacy_dashboard_admin_token(request)
    _ensure_repo_on_path()
    from bot_process_control import restart_trading_bot_after_credential_save  # noqa: PLC0415
    from upstox_credentials_store import (  # noqa: PLC0415
        credentials_file_for_user,
        normalize_access_token,
        persist_credentials_for_user,
        read_credentials_file_for_user,
        sanitize_username,
    )

    if body.for_user and body.for_user.strip() and actor.role != "admin":
        raise HTTPException(status_code=403, detail="Only admin may set for_user.")
    subject = _resolve_credential_subject(actor, body.for_user)
    safe = sanitize_username(subject)

    current = read_credentials_file_for_user(safe)
    updated = False
    if body.access_token.strip():
        current["access_token"] = normalize_access_token(body.access_token)
        updated = True
    if body.api_key.strip():
        current["api_key"] = body.api_key.strip()
        updated = True
    if body.api_secret.strip():
        current["api_secret"] = body.api_secret.strip()
        updated = True
    if body.base_url.strip():
        current["base_url"] = body.base_url.strip()
        updated = True
    persist_credentials_for_user(safe, current)
    restart_result = None
    if updated:
        restart_result = await asyncio.to_thread(restart_trading_bot_after_credential_save)
    log_action(
        actor.username,
        "upstox_settings_saved",
        {"updated": updated, "credential_subject": safe},
        target_user=safe,
    )
    cred_path = credentials_file_for_user(safe)
    return {
        "ok": True,
        "saved": cred_path.name,
        "credential_subject": safe,
        "bot_restart": restart_result
        or {"restarted": False, "skipped": "no credential fields changed"},
    }