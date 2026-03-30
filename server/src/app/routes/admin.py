from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import require_admin_dep, UserClaims
from app.services.audit_log import read_recent_audit_lines

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/audit/{username}")
async def get_user_audit(
    username: str,
    max_lines: int = 200,
    _admin: UserClaims = Depends(require_admin_dep),
):
    if max_lines < 1 or max_lines > 2000:
        raise HTTPException(status_code=400, detail="max_lines must be 1..2000")
    lines = read_recent_audit_lines(username, max_lines=max_lines)
    return {"username": username, "lines": lines, "count": len(lines)}
