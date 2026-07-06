"""Broker credential read/save/test (Upstox + Kite + Groww)."""

from __future__ import annotations

import asyncio

import requests
from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import UserClaims, require_user
from app.models.schemas import BrokerCredentialsBody
from app.services.audit_log import log_action

router = APIRouter(prefix="/api/settings", tags=["settings"])

SUPPORTED = frozenset({"upstox", "kite", "groww"})


def _ensure_repo_on_path() -> None:
    from app.config.paths import ensure_repo_and_lib_on_path

    ensure_repo_and_lib_on_path()


def _normalize_broker(broker: str) -> str:
    b = (broker or "upstox").strip().lower()
    if b not in SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Broker {b!r} not supported yet.")
    return b


def _read_broker_creds(broker: str, username: str) -> dict[str, str]:
    if broker == "kite":
        from kite_credentials_store import read_credentials_file_for_user

        return read_credentials_file_for_user(username)
    if broker == "groww":
        from groww_credentials_store import read_credentials_file_for_user

        return read_credentials_file_for_user(username)
    from upstox_credentials_store import read_credentials_file_for_user

    return read_credentials_file_for_user(username)


def _persist_broker_creds(broker: str, username: str, data: dict[str, str]) -> dict[str, str]:
    if broker == "kite":
        from kite_credentials_store import persist_credentials_for_user

        return persist_credentials_for_user(username, data)
    if broker == "groww":
        from groww_credentials_store import persist_credentials_for_user

        return persist_credentials_for_user(username, data)
    from upstox_credentials_store import normalize_access_token, persist_credentials_for_user

    if data.get("access_token"):
        data["access_token"] = normalize_access_token(data["access_token"])
    return persist_credentials_for_user(username, data)


def _cred_path(broker: str, username: str):
    if broker == "kite":
        from kite_credentials_store import credentials_file_for_user

        return credentials_file_for_user(username)
    if broker == "groww":
        from groww_credentials_store import credentials_file_for_user

        return credentials_file_for_user(username)
    from upstox_credentials_store import credentials_file_for_user

    return credentials_file_for_user(username)


def _mask_tail(value: str, tail: int = 4) -> str:
    from upstox_credentials_store import mask_tail

    return mask_tail(value, tail)


def _sanitize(username: str) -> str:
    from upstox_credentials_store import sanitize_username

    return sanitize_username(username)


@router.get("/credentials")
async def get_broker_credentials(
    broker: str = Query("upstox"),
    user: UserClaims = Depends(require_user),
):
    _ensure_repo_on_path()
    b = _normalize_broker(broker)
    safe = _sanitize(user.username)
    data = _read_broker_creds(b, safe)
    cred_path = _cred_path(b, safe)
    return {
        "broker": b,
        "base_url": data.get("base_url", ""),
        "access_token_preview": _mask_tail(data.get("access_token", "")),
        "api_key_preview": _mask_tail(data.get("api_key", "")),
        "api_secret_preview": _mask_tail(data.get("api_secret", "")),
        "has_access_token": bool(data.get("access_token")),
        "has_api_key": bool(data.get("api_key")),
        "has_api_secret": bool(data.get("api_secret")),
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
    safe = _sanitize(actor.username)
    creds = _read_broker_creds(b, safe)
    access_token = (creds.get("access_token") or "").strip()
    api_key = (creds.get("api_key") or "").strip()
    base_url = (creds.get("base_url") or "").strip()

    if not access_token:
        raise HTTPException(status_code=400, detail=f"No access token saved for {safe} ({b}).")

    if b == "kite":
        if not api_key:
            raise HTTPException(status_code=400, detail=f"No Kite api_key saved for {safe}.")
        url = f"{base_url.rstrip('/')}/user/profile"
        headers = {"Authorization": f"token {api_key}:{access_token}", "Accept": "application/json"}
    elif b == "groww":
        if not base_url:
            base_url = "https://api.groww.in"
        from app.services.groww_token import fetch_user_profile

        try:
            profile = await asyncio.to_thread(
                fetch_user_profile,
                access_token=access_token,
                base_url=base_url,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=f"groww test failed for {safe}: {exc}") from exc
        profile_out = {
            "ucc": profile.get("ucc"),
            "vendor_user_id": profile.get("vendor_user_id"),
            "active_segments": profile.get("active_segments"),
            "nse_enabled": profile.get("nse_enabled"),
            "bse_enabled": profile.get("bse_enabled"),
        }
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
            "tested_endpoint": f"{base_url.rstrip('/')}/v1/user/detail",
            "profile": profile_out,
            "message": f"groww auth check succeeded for {safe}.",
        }
    else:
        if not base_url:
            raise HTTPException(status_code=400, detail=f"No Upstox base URL saved for {safe}.")
        url = f"{base_url.rstrip('/')}/user/profile"
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"{b} connectivity test failed: {exc}") from exc

    payload: dict = {}
    try:
        payload = response.json() if response.text else {}
    except ValueError:
        payload = {}

    if response.status_code != 200:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("error_type") or payload)[:250]
        raise HTTPException(
            status_code=502,
            detail=f"{b} test failed for {safe}: HTTP {response.status_code} - {detail}",
        )

    if b == "kite":
        profile = payload.get("data", {}) if isinstance(payload, dict) else {}
        profile_out = {
            "user_name": profile.get("user_name"),
            "user_id": profile.get("user_id"),
            "email": profile.get("email"),
            "broker": profile.get("broker"),
        }
    else:
        profile = payload.get("data", {}) if isinstance(payload, dict) else {}
        profile_out = {
            "user_name": profile.get("user_name"),
            "email": profile.get("email"),
            "user_id": profile.get("user_id"),
            "broker": profile.get("broker"),
        }

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
        "profile": profile_out,
        "message": f"{b} auth check succeeded for {safe}.",
    }


@router.post("/credentials")
async def post_broker_credentials(
    body: BrokerCredentialsBody,
    actor: UserClaims = Depends(require_user),
):
    _ensure_repo_on_path()
    b = _normalize_broker(body.broker)
    safe = _sanitize(actor.username)
    current = _read_broker_creds(b, safe)
    updated = False
    if body.access_token.strip():
        current["access_token"] = body.access_token.strip()
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
    _persist_broker_creds(b, safe, current)

    restart_result = None
    if updated and b == "upstox":
        from bot_process_control import restart_engine_after_credential_save

        restart_result = await asyncio.to_thread(restart_engine_after_credential_save)

    log_action(
        actor.username,
        "broker_credentials_saved",
        {"updated": updated, "credential_subject": safe, "broker": b},
        target_user=safe,
    )
    cred_path = _cred_path(b, safe)
    bot_restart = (
        restart_result
        if updated and b == "upstox" and restart_result is not None
        else {"restarted": False, "skipped": "no restart" if b != "upstox" else "no credential fields changed"}
    )
    return {
        "ok": True,
        "broker": b,
        "saved": cred_path.name,
        "credential_subject": safe,
        "bot_restart": bot_restart,
    }
