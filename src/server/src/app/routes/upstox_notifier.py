"""Public Upstox notifier webhook (no auth — called by Upstox servers)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.config.paths import ensure_repo_and_lib_on_path
from app.models.schemas import UpstoxTokenNotifierBody
from app.services.audit_log import log_action

logger = logging.getLogger("ak07.upstox_notifier")

router = APIRouter(prefix="/api/upstox", tags=["upstox"])


def _credential_username() -> str:
    ensure_repo_and_lib_on_path()
    from upstox_credentials_store import list_usernames_from_auth_store  # noqa: PLC0415

    users = list_usernames_from_auth_store()
    return users[0] if users else "AK07"


@router.get("/token-notifier")
@router.head("/token-notifier")
async def token_notifier_probe():
    """Upstox / health checks may GET or HEAD the notifier URL before accepting it."""
    return {"ok": True, "endpoint": "upstox-token-notifier"}


@router.post("/token-notifier")
async def receive_upstox_access_token(request: Request):
    """Receive V3 access_token deliveries from Upstox after user approval."""
    raw = await request.body()
    remote = request.client.host if request.client else "?"
    logger.info("Upstox notifier POST from %s (%d bytes)", remote, len(raw))

    try:
        payload = UpstoxTokenNotifierBody.model_validate_json(raw)
    except Exception as exc:
        logger.error("Notifier payload parse failed from %s: %s body=%r", remote, exc, raw[:500])
        raise HTTPException(status_code=400, detail="invalid notifier payload") from exc

    ensure_repo_and_lib_on_path()
    from upstox_credentials_store import (  # noqa: PLC0415
        persist_credentials_for_user,
        read_credentials_file_for_user,
    )

    username = _credential_username()
    creds = read_credentials_file_for_user(username)
    stored_client_id = (creds.get("api_key") or "").strip()
    incoming_client_id = (payload.client_id or "").strip()

    if stored_client_id and incoming_client_id and incoming_client_id != stored_client_id:
        logger.warning(
            "Upstox notifier client_id mismatch (stored=%s… incoming=%s…)",
            stored_client_id[:8],
            incoming_client_id[:8],
        )
        raise HTTPException(status_code=403, detail="client_id mismatch")

    token = (payload.access_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="access_token required")

    if payload.message_type and payload.message_type != "access_token":
        logger.info("Ignoring Upstox notifier message_type=%s", payload.message_type)
        return {"ok": True, "ignored": True}

    persist_credentials_for_user(username, {**creds, "access_token": token})
    log_action(
        username,
        "upstox_token_notifier",
        {
            "client_id_tail": incoming_client_id[-4:] if incoming_client_id else "",
            "user_id": payload.user_id or "",
            "expires_at": payload.expires_at or "",
            "remote": request.client.host if request.client else "",
        },
    )
    logger.info(
        "Upstox access token persisted via notifier webhook (user=%s, len=%d)",
        username,
        len(token),
    )
    return {"ok": True, "saved": True}
