"""Admin user management API."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

from app.constants import ALL_STRATEGIES, STRATEGY_LABELS
from app.dependencies import UserClaims, require_admin_user
from app.models.schemas import (
    AdminBlrUpdateBody,
    CreateUserBody,
    UpdateUserProfileBody,
    UserProfilePublic,
    UserPublic,
)
from app.services.admin_blr import get_blr_state, update_blr_levels
from app.services.audit_log import log_action
from app.services.broker_connection_status import broker_connection_status
from app.services.performance_store import load_s3_trades, s3_trade_log_rows
from app.services.user_profiles_store import read_profile, write_profile
from app.services.users_store import create_user, get_user_record, list_users

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _profile_public(username: str, role: str, prof: dict) -> UserProfilePublic:
    from app.services.user_profiles_store import lots_for_strategy, normalize_strategy_lots

    fallback = lots_for_strategy(prof, "s3_breakout")
    return UserProfilePublic(
        username=username,
        role=role,
        enabled_strategies=prof.get("enabled_strategies") or [],
        broker=str(prof.get("broker") or "upstox"),
        paper_trading=bool(prof.get("paper_trading")),
        lots=fallback,
        strategy_lots=normalize_strategy_lots(prof.get("strategy_lots"), default=fallback),
        egress_ip=str(prof.get("egress_ip") or "").strip(),
    )


@router.get("/blr")
async def admin_get_blr(
    index_code: str = "NIFTY",
    admin: UserClaims = Depends(require_admin_user),
):
    del admin
    code = index_code.strip().upper()
    if code not in ("NIFTY", "BANKNIFTY", "SENSEX"):
        raise HTTPException(status_code=400, detail="Unknown index.")
    return {"state": get_blr_state(code)}


@router.post("/blr")
async def admin_update_blr(
    body: AdminBlrUpdateBody,
    admin: UserClaims = Depends(require_admin_user),
):
    try:
        state = update_blr_levels(
            index_code=body.index_code,
            green=body.green,
            mid=body.mid,
            red=body.red,
            updated_by=admin.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_action(
        admin.username,
        "blr_levels_updated",
        {
            "index": body.index_code,
            "green": body.green,
            "mid": body.mid,
            "red": body.red,
        },
    )
    return {"ok": True, "state": state}


@router.get("/broker-status")
def admin_broker_status(admin: UserClaims = Depends(require_admin_user)):
    del admin
    users = list_users()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(users)))) as pool:
        statuses = list(
            pool.map(
                lambda row: broker_connection_status(
                    str(row.get("username") or ""),
                    str(row.get("role") or "user"),
                ),
                users,
            )
        )
    return {"statuses": statuses}


@router.get("/s3-trades")
async def admin_s3_trades(
    days: int = 14,
    admin: UserClaims = Depends(require_admin_user),
):
    del admin
    lookback = max(1, min(int(days), 90))
    end = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    start = end - timedelta(days=lookback - 1)
    trades = load_s3_trades(start_date=start, end_date=end)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "trades": trades,
        "rows": s3_trade_log_rows(trades),
    }


@router.get("/users")
async def admin_list_users(admin: UserClaims = Depends(require_admin_user)):
    rows = []
    for u in list_users():
        prof = read_profile(u["username"], role=u["role"])
        rows.append(
            {
                **u,
                "profile": _profile_public(u["username"], u["role"], prof),
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
            lots=body.lots,
            strategy_lots=body.strategy_lots or None,
            egress_ip=body.egress_ip,
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
        "profile": _profile_public(rec["username"], rec["role"], prof),
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
    return _profile_public(username, str(rec.get("role") or "user"), prof)
