"""Unified broker credential read/save/test for Upstox and Zerodha (Kite)."""

from __future__ import annotations

import asyncio
from typing import Literal

import requests
from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import UserClaims, require_user
from app.models.schemas import BrokerCredentialsBody
from app.services.audit_log import log_action

router = APIRouter(prefix="/api/settings", tags=["settings"])

BrokerName = Literal["upstox", "zerodha"]


def _ensure_repo_on_path() -> None:
    from app.config.paths import ensure_repo_and_lib_on_path

    ensure_repo_and_lib_on_path()


def _normalize_broker(b: str) -> BrokerName:
    x = (b or "upstox").strip().lower()
    if x not in ("upstox", "zerodha"):
        raise HTTPException(status_code=400, detail="broker must be upstox or zerodha")
    return x  # type: ignore[return-value]


def _credential_helpers(broker: BrokerName):
    if broker == "zerodha":
        from zerodha_credentials_store import (  # noqa: PLC0415
            credentials_file_for_user,
            persist_credentials_for_user,
            read_credentials_file_for_user,
        )
    else:
        from upstox_credentials_store import (  # noqa: PLC0415
            credentials_file_for_user,
            persist_credentials_for_user,
            read_credentials_file_for_user,
        )
    from upstox_credentials_store import mask_tail, normalize_access_token, sanitize_username  # noqa: PLC0415

    return (
        credentials_file_for_user,
        mask_tail,
        normalize_access_token,
        persist_credentials_for_user,
        read_credentials_file_for_user,
        sanitize_username,
    )


@router.get("/credentials")
async def get_broker_credentials(
    broker: str = Query("upstox"),
    user: UserClaims = Depends(require_user),
):
    _ensure_repo_on_path()
    b = _normalize_broker(broker)
    (
        credentials_file_for_user,
        mask_tail,
        _normalize_access_token,
        _persist,
        read_credentials_file_for_user,
        sanitize_username,
    ) = _credential_helpers(b)

    safe = sanitize_username(user.username)
    data = read_credentials_file_for_user(safe)
    cred_path = credentials_file_for_user(safe)
    return {
        "broker": b,
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
    }


@router.post("/credentials/test")
async def test_broker_credentials(
    broker: str = Query("upstox"),
    actor: UserClaims = Depends(require_user),
):
    _ensure_repo_on_path()
    b = _normalize_broker(broker)
    (
        _cf,
        _mask_tail,
        _norm,
        _persist,
        read_credentials_file_for_user,
        sanitize_username,
    ) = _credential_helpers(b)

    safe = sanitize_username(actor.username)
    creds = read_credentials_file_for_user(safe)
    access_token = (creds.get("access_token") or "").strip()
    base_url = (creds.get("base_url") or "").strip()
    api_key = (creds.get("api_key") or "").strip()

    if b == "upstox":
        if not access_token:
            raise HTTPException(status_code=400, detail=f"No Upstox access token saved for {safe}.")
        if not base_url:
            raise HTTPException(status_code=400, detail=f"No Upstox base URL saved for {safe}.")
        url = f"{base_url.rstrip('/')}/user/profile"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        try:
            response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Upstox connectivity test failed: {exc}") from exc
        payload: dict = {}
        try:
            payload = response.json() if response.text else {}
        except ValueError:
            payload = {}
        if response.status_code != 200:
            broker_message = ""
            if isinstance(payload, dict):
                errors = payload.get("errors") or []
                if errors and isinstance(errors, list):
                    first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
                    broker_message = first.get("message") or str(first)
                broker_message = broker_message or payload.get("message", "")
            detail = broker_message or (response.text or "").strip()[:250] or "Unknown Upstox error"
            raise HTTPException(
                status_code=502,
                detail=f"Upstox test failed for {safe}: HTTP {response.status_code} - {detail}",
            )
        profile = payload.get("data", {}) if isinstance(payload, dict) else {}
        log_action(
            actor.username,
            "broker_credentials_tested",
            {"credential_subject": safe, "broker": b, "ok": True},
            target_user=safe,
        )
        return {
            "ok": True,
            "broker": b,
            "credential_subject": safe,
            "base_url": base_url,
            "tested_endpoint": url,
            "profile": {
                "user_name": profile.get("user_name"),
                "email": profile.get("email"),
                "user_id": profile.get("user_id"),
                "broker": profile.get("broker"),
            },
            "message": f"Upstox auth check succeeded for {safe}.",
        }

    if not api_key:
        raise HTTPException(status_code=400, detail=f"No Zerodha API key saved for {safe}.")
    if not access_token:
        raise HTTPException(status_code=400, detail=f"No Zerodha access token saved for {safe}.")
    kite_base = base_url or "https://api.kite.trade"
    url = f"{kite_base.rstrip('/')}/user/profile"
    headers = {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }
    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Zerodha connectivity test failed: {exc}") from exc
    try:
        payload = response.json() if response.text else {}
    except ValueError:
        payload = {}
    if response.status_code != 200:
        err = ""
        if isinstance(payload, dict):
            err = str(payload.get("message") or payload.get("error_type") or "")
        detail = err or (response.text or "").strip()[:250] or "Unknown Kite error"
        raise HTTPException(
            status_code=502,
            detail=f"Zerodha (Kite) test failed for {safe}: HTTP {response.status_code} - {detail}",
        )
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    log_action(
        actor.username,
        "broker_credentials_tested",
        {"credential_subject": safe, "broker": b, "ok": True},
        target_user=safe,
    )
    return {
        "ok": True,
        "broker": b,
        "credential_subject": safe,
        "base_url": kite_base,
        "tested_endpoint": url,
        "profile": {
            "user_name": data.get("user_name"),
            "email": data.get("email"),
            "user_id": data.get("user_id"),
            "broker": "zerodha",
        },
        "message": f"Kite auth check succeeded for {safe}.",
    }


@router.post("/credentials")
async def post_broker_credentials(
    body: BrokerCredentialsBody,
    actor: UserClaims = Depends(require_user),
):
    _ensure_repo_on_path()
    b = _normalize_broker(body.broker)
    (
        credentials_file_for_user,
        _mask_tail,
        normalize_access_token,
        persist_credentials_for_user,
        read_credentials_file_for_user,
        sanitize_username,
    ) = _credential_helpers(b)

    safe = sanitize_username(actor.username)
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
    if updated and b == "upstox":
        from bot_process_control import restart_trading_bot_after_credential_save  # noqa: PLC0415

        restart_result = await asyncio.to_thread(restart_trading_bot_after_credential_save)

    log_action(
        actor.username,
        "broker_credentials_saved",
        {"updated": updated, "credential_subject": safe, "broker": b},
        target_user=safe,
    )
    cred_path = credentials_file_for_user(safe)
    if updated and b == "upstox":
        bot_restart = restart_result or {"restarted": False, "skipped": "no credential fields changed"}
    elif updated and b == "zerodha":
        bot_restart = {"restarted": False, "skipped": "Zerodha saved; live bot still uses Upstox API only"}
    else:
        bot_restart = {"restarted": False, "skipped": "no credential fields changed"}
    return {
        "ok": True,
        "broker": b,
        "saved": cred_path.name,
        "credential_subject": safe,
        "bot_restart": bot_restart,
    }
