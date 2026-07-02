"""Admin user management API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.constants import ALL_STRATEGIES, STRATEGY_LABELS
from app.dependencies import UserClaims, require_admin_user
from app.models.schemas import CreateUserBody, UpdateUserProfileBody, UserProfilePublic, UserPublic
from app.services.audit_log import log_action
from app.services.user_profiles_store import read_profile, write_profile
from app.services.users_store import create_user, get_user_record, list_users

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users")
async def admin_list_users(admin: UserClaims = Depends(require_admin_user)):
    rows = []
    for u in list_users():
        prof = read_profile(u["username"], role=u["role"])
        rows.append(
            {
                **u,
                "profile": UserProfilePublic(
                    username=prof["username"],
                    role=u["role"],
                    enabled_strategies=prof.get("enabled_strategies") or [],
                    broker=str(prof.get("broker") or "upstox"),
                    paper_trading=bool(prof.get("paper_trading")),
                ),
            }
        )
    return {"users": rows, "strategies": [{"id": s, "label": STRATEGY_LABELS[s]} for s in ALL_STRATEGIES]}


@router.post("/users")
async def admin_create_user(body: CreateUserBody, admin: UserClaims = Depends(require_admin_user)):
    try:
        rec = create_user(
            body.username,
            body.password,
            role=body.role,
            enabled_strategies=body.enabled_strategies or None,
            broker=body.broker,
            paper_trading=body.paper_trading,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_action(
        admin.username,
        "user_created",
        {"username": rec["username"], "role": rec["role"]},
        target_user=rec["username"],
    )
    prof = rec["profile"]
    return {
        "ok": True,
        "user": UserPublic(username=rec["username"], role=rec["role"]),
        "profile": UserProfilePublic(
            username=rec["username"],
            role=rec["role"],
            enabled_strategies=prof.get("enabled_strategies") or [],
            broker=str(prof.get("broker") or "upstox"),
            paper_trading=bool(prof.get("paper_trading")),
        ),
    }


@router.patch("/users/{username}/profile")
async def admin_update_profile(
    username: str,
    body: UpdateUserProfileBody,
    admin: UserClaims = Depends(require_admin_user),
):
    if not get_user_record(username):
        raise HTTPException(status_code=404, detail="User not found.")
    payload = body.model_dump(exclude_unset=True)
    prof = write_profile(username, payload)
    log_action(
        admin.username,
        "user_profile_updated",
        {"fields": list(payload.keys())},
        target_user=username,
    )
    rec = get_user_record(username) or {"username": username, "role": "user"}
    return UserProfilePublic(
        username=username,
        role=str(rec.get("role") or "user"),
        enabled_strategies=prof.get("enabled_strategies") or [],
        broker=str(prof.get("broker") or "upstox"),
        paper_trading=bool(prof.get("paper_trading")),
    )
