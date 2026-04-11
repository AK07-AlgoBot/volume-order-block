"""Kite Connect OAuth: start URL + redirect callback (token exchange, save zerodha_credentials.json)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from urllib.parse import parse_qs, quote, unquote, urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config.paths import ensure_repo_and_lib_on_path
from app.config.settings import get_settings
from app.dependencies import UserClaims, require_user
from app.services.audit_log import log_action
from app.services.kite_oauth_pending import consume_pending, create_pending

logger = logging.getLogger(__name__)


def _setup_kite_oauth_logging() -> None:
    """Ensure INFO logs go to data/logs/kite_oauth.log (and propagate to uvicorn console when enabled)."""
    if getattr(_setup_kite_oauth_logging, "_done", False):
        return
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    try:
        from app.config.paths import server_root

        log_path = server_root() / "data" / "logs" / "kite_oauth.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    setattr(_setup_kite_oauth_logging, "_done", True)


_setup_kite_oauth_logging()

router_api = APIRouter(prefix="/api/auth", tags=["auth"])
router_public = APIRouter(tags=["kite"])


def _mask_secret(value: str, head: int = 4, tail: int = 4) -> str:
    v = (value or "").strip()
    if len(v) <= head + tail:
        return "***" if v else ""
    return f"{v[:head]}…{v[-tail:]}"


def _log_query_safe(q) -> dict[str, str]:
    """Query param keys + masked values for logs (never log full request_token)."""
    out: dict[str, str] = {}
    for key in q.keys():
        raw = (q.get(key) or "").strip()
        if key == "request_token" and raw:
            out[key] = _mask_secret(raw)
        elif key in ("api_key", "checksum") and raw:
            out[key] = _mask_secret(raw, 3, 3)
        else:
            out[key] = raw[:500] if len(raw) <= 500 else raw[:500] + "…"
    return out


def _frontend_base() -> str:
    s = get_settings()
    if s.kite_post_login_redirect.strip():
        return s.kite_post_login_redirect.strip().rstrip("/")
    if s.cors_origins:
        return str(s.cors_origins[0]).rstrip("/")
    return "http://localhost:5173"


def _redirect_frontend(**params: str) -> RedirectResponse:
    base = _frontend_base()
    sep = "&" if "?" in base else "?"
    q = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    return RedirectResponse(url=f"{base}{sep}{q}", status_code=302)


@router_api.get("/kite/oauth/start")
async def kite_oauth_start(user: UserClaims = Depends(require_user)):
    """Return Kite login URL (api_key + redirect_params). Requires KITE_* env on server."""
    ensure_repo_and_lib_on_path()
    s = get_settings()
    if not s.kite_api_key or not s.kite_api_secret:
        raise HTTPException(
            status_code=503,
            detail="Set KITE_API_KEY and KITE_API_SECRET on the server (e.g. repo root .env).",
        )
    tid = create_pending(user.username)
    inner = f"dashboard_oauth={tid}"
    login_url = "https://kite.zerodha.com/connect/login?" + urlencode(
        {"v": "3", "api_key": s.kite_api_key, "redirect_params": inner}
    )
    logger.info(
        "Kite OAuth start: dashboard_user=%s pending_oauth=%s api_key=%s",
        user.username,
        _mask_secret(tid, 6, 6),
        _mask_secret(s.kite_api_key, 4, 4) if s.kite_api_key else "",
    )
    return {"login_url": login_url, "hint": "Open login_url in this browser; after login you return to the API callback then the dashboard."}


@router_public.get("/kite/callback")
async def kite_oauth_callback(request: Request):
    """Registered redirect URL for Kite Connect — must match KITE_REDIRECT_URL / Kite console exactly."""
    ensure_repo_and_lib_on_path()
    s = get_settings()
    api_key = s.kite_api_key
    api_secret = s.kite_api_secret

    q = request.query_params
    client = request.client.host if request.client else ""
    logger.info(
        "Kite OAuth callback hit: client=%s query=%s",
        client,
        _log_query_safe(q),
    )
    logger.info(
        "Note: Zerodha 2FA runs on kite.zerodha.com; this log only runs after redirect to /kite/callback.",
    )

    request_token = q.get("request_token")
    action = q.get("action")
    status = q.get("status")
    st = (status or "").lower()
    if st == "error" or (action and action.lower() == "login" and st and st != "success"):
        logger.warning(
            "Kite OAuth: Zerodha reported login failure action=%s status=%s",
            action,
            status,
        )
        return _redirect_frontend(kite_error="zerodha_login_failed")

    token = (request_token or "").strip()
    tid = (q.get("dashboard_oauth") or "").strip()
    if not tid:
        rp = (q.get("redirect_params") or "").strip()
        if rp:
            tid = (parse_qs(unquote(rp)).get("dashboard_oauth") or [""])[0].strip()
    if not token:
        logger.warning("Kite OAuth: missing request_token in callback query")
        return _redirect_frontend(kite_error="missing_request_token")

    username = consume_pending(tid)
    if not username:
        logger.warning(
            "Kite OAuth: no pending session for oauth_id=%s (expired or invalid)",
            _mask_secret(tid, 6, 6),
        )
        return _redirect_frontend(kite_error="invalid_or_expired_oauth_session")

    if not api_key or not api_secret:
        logger.error("Kite OAuth: KITE_API_KEY or KITE_API_SECRET missing on server")
        return _redirect_frontend(kite_error="server_missing_kite_keys")

    checksum = hashlib.sha256(f"{api_key}{token}{api_secret}".encode()).hexdigest()

    def _post_token():
        return requests.post(
            "https://api.kite.trade/session/token",
            headers={"X-Kite-Version": "3"},
            data={"api_key": api_key, "request_token": token, "checksum": checksum},
            timeout=45,
        )

    logger.info(
        "Kite OAuth: exchanging request_token for access_token (user=%s request_token=%s)",
        username,
        _mask_secret(token),
    )
    try:
        resp = await asyncio.to_thread(_post_token)
    except requests.RequestException as exc:
        logger.exception("Kite OAuth: POST session/token failed: %s", exc)
        log_action(
            username,
            "kite_oauth_failed",
            {"reason": "request_exception", "error": str(exc)[:200]},
            target_user=username,
        )
        return _redirect_frontend(kite_error="token_exchange_failed")

    try:
        payload = resp.json() if resp.text else {}
    except ValueError:
        payload = {}
    logger.info(
        "Kite OAuth: session/token HTTP %s kite_status=%s",
        resp.status_code,
        payload.get("status") if isinstance(payload, dict) else None,
    )
    if resp.status_code != 200 or not isinstance(payload, dict) or payload.get("status") != "success":
        err = ""
        if isinstance(payload.get("message"), str):
            err = payload["message"]
        elif isinstance(payload.get("error_type"), str):
            err = payload["error_type"]
        detail = err or (resp.text or "").strip()[:200] or "unknown"
        logger.warning("Kite OAuth: token exchange rejected detail=%s", detail)
        log_action(
            username,
            "kite_oauth_failed",
            {"reason": "bad_response", "http": resp.status_code, "detail": detail[:200]},
            target_user=username,
        )
        return _redirect_frontend(kite_error="token_exchange_failed")

    data = payload.get("data") or {}
    access_token = (data.get("access_token") or "").strip()
    if not access_token:
        logger.error("Kite OAuth: success payload but no access_token in data")
        return _redirect_frontend(kite_error="missing_access_token")

    from upstox_credentials_store import normalize_access_token, sanitize_username
    from zerodha_credentials_store import persist_credentials_for_user, read_credentials_file_for_user

    safe = sanitize_username(username)
    current = read_credentials_file_for_user(safe)
    current["access_token"] = normalize_access_token(access_token)
    current["api_key"] = api_key
    current["api_secret"] = api_secret
    if not (current.get("base_url") or "").strip():
        current["base_url"] = "https://api.kite.trade"
    persist_credentials_for_user(safe, current)

    kite_user = (data.get("user_id") or data.get("user_name") or "").strip()
    logger.info(
        "Kite OAuth completed: dashboard_user=%s credential_subject=%s kite_user_id=%s access_token=%s",
        username,
        safe,
        kite_user or "(not in response)",
        _mask_secret(access_token),
    )
    log_action(
        username,
        "kite_oauth_completed",
        {"credential_subject": safe, "kite_user_id": kite_user or None},
        target_user=safe,
    )
    return _redirect_frontend(kite="connected")
