"""Authenticated read-only access to orders.log (tail)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.constants import DASHBOARD_USERNAME
from app.dependencies import UserClaims, require_user
from app.services.orders_log_reader import tail_orders_log
from upstox_credentials_store import user_data_dir

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/orders")
async def get_orders_log_tail(
    _auth: UserClaims = Depends(require_user),
    max_lines: int = Query(500, ge=1, le=10_000, description="Max lines from end of file"),
):
    path = user_data_dir(DASHBOARD_USERNAME) / "logs" / "orders.log"
    lines, truncated = tail_orders_log(path, max_lines)
    return {
        "lines": lines,
        "truncated": truncated,
        "line_count": len(lines),
        "path": f"users/{DASHBOARD_USERNAME}/logs/orders.log",
    }


@router.get("/paper")
async def get_paper_orders_log_tail(
    _auth: UserClaims = Depends(require_user),
    max_lines: int = Query(500, ge=1, le=10_000, description="Max lines from end of file"),
):
    path = user_data_dir(DASHBOARD_USERNAME) / "logs" / "paper_orders.log"
    lines, truncated = tail_orders_log(path, max_lines)
    return {
        "lines": lines,
        "truncated": truncated,
        "line_count": len(lines),
        "path": f"users/{DASHBOARD_USERNAME}/logs/paper_orders.log",
    }
