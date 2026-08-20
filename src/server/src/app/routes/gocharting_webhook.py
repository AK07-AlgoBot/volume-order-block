"""GoCharting Lipi webhook — plain-text POST, secret on the query string."""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger("ak07.gocharting.webhook")

router = APIRouter(prefix="/api/gocharting", tags=["gocharting"])


def _configured_secret() -> str:
    return (os.environ.get("AK07_GC_WEBHOOK_SECRET") or "").strip()


def _incoming_token(request: Request, token: str | None) -> str:
    if token and token.strip():
        return token.strip()
    header = (request.headers.get("x-ak07-token") or "").strip()
    if header:
        return header
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _authorize(request: Request, token: str | None) -> None:
    expected = _configured_secret()
    if not expected:
        raise HTTPException(status_code=503, detail="gocharting webhook secret not configured")
    incoming = _incoming_token(request, token)
    if not incoming or not secrets.compare_digest(incoming, expected):
        raise HTTPException(status_code=403, detail="invalid webhook token")


@router.get("/alert")
@router.head("/alert")
async def gocharting_alert_probe(request: Request, token: str | None = Query(default=None)):
    _authorize(request, token)
    return {"ok": True, "endpoint": "gocharting-alert"}


@router.post("/alert")
async def receive_gocharting_alert(
    request: Request,
    token: str | None = Query(default=None),
    index: str | None = Query(default=None),
):
    _authorize(request, token)
    raw_bytes = await request.body()
    raw = raw_bytes.decode("utf-8", errors="replace")
    remote = request.client.host if request.client else "?"
    logger.info("GoCharting alert from %s (%d bytes)", remote, len(raw_bytes))

    from app.services.gocharting_oms import enqueue_gocharting_alert  # noqa: PLC0415

    header_index = (
        request.headers.get("x-symbol")
        or request.headers.get("x-ticker")
        or ""
    )
    result = enqueue_gocharting_alert(
        raw,
        default_index=(index or header_index or "").strip(),
    )
    if result.get("reason") == "redis queue failed":
        raise HTTPException(status_code=503, detail="alert queue unavailable")
    if not result.get("ok"):
        logger.warning("GoCharting alert ignored from %s: %s body=%r", remote, result, raw[:300])
    return result
