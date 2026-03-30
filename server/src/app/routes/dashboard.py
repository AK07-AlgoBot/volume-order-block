from fastapi import APIRouter, Depends, Query

from app.dependencies import UserClaims, require_user, trade_context_for
from app.services.trade_context import TradeUserContext

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/initial")
async def dashboard_initial(
    user: UserClaims = Depends(require_user),
    view_as: str | None = Query(None, description="Admin only: load another user's data"),
):
    ctx = trade_context_for(user, view_as)
    return ctx.dashboard_initial_dict()


@router.get("/symbol-performance")
async def dashboard_symbol_performance(
    days: int = 14,
    user: UserClaims = Depends(require_user),
    view_as: str | None = Query(None),
):
    ctx = trade_context_for(user, view_as)
    try:
        safe_days = int(days)
    except (TypeError, ValueError):
        safe_days = 14
    return ctx._compute_symbol_performance(safe_days)


@router.get("/closed-trades")
async def dashboard_closed_trades(
    date: str | None = None,
    user: UserClaims = Depends(require_user),
    view_as: str | None = Query(None),
):
    ctx = trade_context_for(user, view_as)
    return ctx.closed_trades_response(date)


@router.get("/weekly-pnl")
async def dashboard_weekly_pnl(
    week_offset: int = 0,
    user: UserClaims = Depends(require_user),
    view_as: str | None = Query(None),
):
    ctx = trade_context_for(user, view_as)
    return ctx.weekly_pnl_dict(week_offset)


@router.get("/monthly-pnl")
async def dashboard_monthly_pnl(
    month_offset: int = 0,
    user: UserClaims = Depends(require_user),
    view_as: str | None = Query(None),
):
    ctx = trade_context_for(user, view_as)
    return ctx.monthly_pnl_dict(month_offset)
