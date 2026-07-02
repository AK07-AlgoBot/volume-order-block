"""Kite Connect OAuth — one-click Zerodha login from the dashboard."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.dependencies import UserClaims, require_user
from app.models.schemas import KiteConnectStartBody
from app.services.kite_oauth import (
    COOKIE_COCKPIT,
    COOKIE_NAME,
    COOKIE_RESUME,
    KITE_LOGIN_URL,
    cockpit_return_url,
    complete_oauth,
    consume_connect_ott,
    create_connect_ott,
    kite_redirect_url,
)

router = APIRouter(prefix="/api/brokers/kite", tags=["kite"])


def _api_public_base(request: Request) -> str:
    explicit = (os.environ.get("AK07_API_PUBLIC_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.post("/connect/start")
async def kite_connect_start(
    request: Request,
    body: KiteConnectStartBody | None = None,
    user: UserClaims = Depends(require_user),
):
    from kite_credentials_store import read_credentials_file_for_user

    creds = read_credentials_file_for_user(user.username)
    if not (creds.get("api_key") or "").strip():
        raise HTTPException(status_code=400, detail="Save your Kite api_key first.")
    if not (creds.get("api_secret") or "").strip():
        raise HTTPException(status_code=400, detail="Save your Kite api_secret first.")
    cockpit_url = (body.cockpit_url if body else None) or ""
    ott = create_connect_ott(user.username, cockpit_url=cockpit_url)
    base = _api_public_base(request)
    connect_url = f"{base}/api/brokers/kite/connect?ott={ott}"
    return {
        "ok": True,
        "connect_url": connect_url,
        "redirect_url_registered": kite_redirect_url(),
        "message": "Open connect_url in your browser to log in to Zerodha.",
    }


@router.get("/connect")
async def kite_connect(ott: str = Query(..., min_length=8)):
    from kite_credentials_store import read_credentials_file_for_user

    ctx = consume_connect_ott(ott)
    if not ctx:
        raise HTTPException(status_code=400, detail="Connect link expired — start again from the dashboard.")
    username = ctx["username"]
    creds = read_credentials_file_for_user(username)
    api_key = (creds.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Kite api_key missing for this user.")
    login_url = KITE_LOGIN_URL.format(api_key=api_key)
    response = RedirectResponse(url=login_url, status_code=302)
    cookie_opts = {"max_age": 600, "httponly": True, "samesite": "lax"}
    response.set_cookie(key=COOKIE_NAME, value=username, **cookie_opts)
    response.set_cookie(key=COOKIE_COCKPIT, value=ctx["cockpit_url"], **cookie_opts)
    if ctx.get("resume_token"):
        response.set_cookie(key=COOKIE_RESUME, value=ctx["resume_token"], **cookie_opts)
    return response


@router.get("/callback")
async def kite_callback(
    request: Request,
    request_token: str = Query(default=""),
    status: str = Query(default=""),
):
    username = request.cookies.get(COOKIE_NAME, "").strip()
    cockpit_base = request.cookies.get(COOKIE_COCKPIT, "").strip()
    resume_token = request.cookies.get(COOKIE_RESUME, "").strip()

    def _return(*, success: bool, detail: str = "") -> RedirectResponse:
        return RedirectResponse(
            cockpit_return_url(
                success=success,
                detail=detail,
                cockpit_base=cockpit_base,
                resume_token=resume_token if success else "",
            ),
            status_code=302,
        )

    if not username:
        return _return(success=False, detail="session_expired")
    if status and status.lower() != "success":
        return _return(success=False, detail=f"kite_status_{status}")
    if not request_token.strip():
        return _return(success=False, detail="missing_request_token")
    try:
        complete_oauth(username, request_token.strip())
    except Exception as exc:
        return _return(success=False, detail=str(exc)[:120])
    response = _return(success=True)
    for name in (COOKIE_NAME, COOKIE_COCKPIT, COOKIE_RESUME):
        response.delete_cookie(name)
    return response
