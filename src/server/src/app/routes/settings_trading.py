"""Trading symbol scope (AK07 only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import UserClaims, require_user
from app.models.schemas import TradingScriptsBody

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _ensure_repo_on_path() -> None:
    from app.config.paths import ensure_repo_and_lib_on_path

    ensure_repo_and_lib_on_path()


@router.get("/trading-scripts")
async def get_trading_scripts(user: UserClaims = Depends(require_user)):
    _ensure_repo_on_path()
    from trading_preferences_store import read_trading_preferences  # noqa: PLC0415
    from trading_script_constants import AVAILABLE_SCRIPT_NAMES  # noqa: PLC0415

    prefs = read_trading_preferences(user.username)
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

    try:
        write_trading_preferences(actor.username, body.enabled_scripts)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"ok": True, "credential_subject": actor.username}
