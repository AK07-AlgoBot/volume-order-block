"""Per-user enabled trading symbols (daily scope)."""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import UserClaims, require_user
from app.models.schemas import TradingScriptsBody
from app.services.users_store import get_user_record

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _ensure_repo_on_path() -> None:
    from pathlib import Path

    from app.config.settings import repo_root

    r = str(repo_root())
    if r not in sys.path:
        sys.path.insert(0, r)


def _resolve_subject(actor: UserClaims, for_user: str | None) -> str:
    target = (for_user or "").strip()
    if not target:
        return actor.username
    if actor.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin may use for_user for another account's trading scope.",
        )
    rec = get_user_record(target)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Unknown user: {target}")
    return target


@router.get("/trading-scripts")
async def get_trading_scripts(
    user: UserClaims = Depends(require_user),
    for_user: str | None = Query(None, description="Admin: read another user's scope"),
):
    _ensure_repo_on_path()
    from trading_preferences_store import read_trading_preferences  # noqa: PLC0415
    from trading_script_constants import AVAILABLE_SCRIPT_NAMES  # noqa: PLC0415

    subject = _resolve_subject(user, for_user)
    prefs = read_trading_preferences(subject)
    ens = prefs.get("enabled_scripts")
    return {
        "available_scripts": list(AVAILABLE_SCRIPT_NAMES),
        "enabled_scripts": ens,
        "mode": "all" if ens is None else "subset",
    }


@router.put("/trading-scripts")
async def put_trading_scripts(
    body: TradingScriptsBody,
    actor: UserClaims = Depends(require_user),
):
    _ensure_repo_on_path()
    from trading_preferences_store import write_trading_preferences  # noqa: PLC0415

    subject = _resolve_subject(actor, body.for_user)
    try:
        write_trading_preferences(subject, body.enabled_scripts)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"ok": True, "credential_subject": subject}
