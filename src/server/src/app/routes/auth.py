from fastapi import APIRouter, Depends, HTTPException, Request

from app.dependencies import UserClaims, require_user
from app.models.schemas import KiteResumeBody, LoginBody, TokenResponse, UserPublic
from app.services.audit_log import log_action
from app.services.user_profiles_store import ensure_profile
from app.services.users_store import authenticate, get_user_record
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
    profile = ensure_profile(rec["username"], role=rec["role"])
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


@router.post("/kite-resume", response_model=TokenResponse)
async def kite_resume(body: KiteResumeBody):
    from app.services.kite_oauth import consume_resume_token

    username = consume_resume_token(body.token.strip())
    if not username:
        raise HTTPException(status_code=400, detail="Resume link expired — sign in manually.")
    rec = get_user_record(username)
    if not rec:
        raise HTTPException(status_code=404, detail="User not found.")
    token = create_access_token(rec["username"], rec["role"])
    ensure_profile(rec["username"], role=rec["role"])
    return TokenResponse(
        access_token=token,
        username=rec["username"],
        role=rec["role"],
    )


@router.get("/profile")
async def profile(user: UserClaims = Depends(require_user)):
    from app.models.schemas import UserProfilePublic

    prof = ensure_profile(user.username, role=user.role)
    return UserProfilePublic(
        username=user.username,
        role=user.role,
        enabled_strategies=prof.get("enabled_strategies") or [],
        broker=str(prof.get("broker") or "upstox"),
        paper_trading=bool(prof.get("paper_trading")),
    )


@router.get("/me", response_model=UserPublic)
async def me(user: UserClaims = Depends(require_user)):
    return UserPublic(username=user.username, role=user.role)
