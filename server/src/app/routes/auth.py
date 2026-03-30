from fastapi import APIRouter, Depends, HTTPException, Request

from app.dependencies import UserClaims, require_admin_dep, require_user
from app.models.schemas import LoginBody, TokenResponse, UserPublic
from app.services.audit_log import log_action
from app.services.users_store import authenticate, list_usernames
from app.utils.security import create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginBody, request: Request):
    rec = authenticate(body.username, body.password)
    if not rec:
        log_action(
            "_system",
            "login_failed",
            {
                "attempted_user": body.username.strip(),
                "ip": request.client.host if request.client else None,
            },
        )
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = create_access_token(rec["username"], rec["role"])
    log_action(
        rec["username"],
        "login_ok",
        {"ip": request.client.host if request.client else None},
    )
    return TokenResponse(
        access_token=token,
        username=rec["username"],
        role=rec["role"],
    )


@router.get("/me", response_model=UserPublic)
async def me(user: UserClaims = Depends(require_user)):
    return UserPublic(username=user.username, role=user.role)


@router.get("/users", response_model=list[UserPublic])
async def list_users(_admin: UserClaims = Depends(require_admin_dep)):
    names = list_usernames()
    from app.services.users_store import get_user_record

    out = []
    for n in names:
        r = get_user_record(n)
        if r:
            out.append(UserPublic(username=str(r["username"]), role=str(r.get("role", "user"))))
    return out
