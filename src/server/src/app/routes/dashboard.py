from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import UserClaims, require_user, trade_context_for
from app.models.schemas import ManualEntryUpdateBody, ManualTradeRemoveBody
from app.services.audit_log import read_recent_audit_lines
from app.services.trade_context import TradeUserContext

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/initial")
async def dashboard_initial(user: UserClaims = Depends(require_user)):
    ctx = trade_context_for(user)
    return ctx.dashboard_initial_dict()


@router.get("/symbol-performance")
async def dashboard_symbol_performance(
    days: int = 14,
    user: UserClaims = Depends(require_user),
):
    ctx = trade_context_for(user)
    try:
        safe_days = int(days)
    except (TypeError, ValueError):
        safe_days = 14
    return ctx._compute_symbol_performance(safe_days)


@router.get("/closed-trades")
async def dashboard_closed_trades(
    date: str | None = None,
    user: UserClaims = Depends(require_user),
):
    ctx = trade_context_for(user)
    return ctx.closed_trades_response(date)


@router.get("/paper-closed-trades")
async def dashboard_paper_closed_trades(
    date: str | None = None,
    user: UserClaims = Depends(require_user),
):
    ctx = trade_context_for(user)
    return ctx.paper_closed_trades_response(date)


@router.get("/paper-live-trades")
async def dashboard_paper_live_trades(user: UserClaims = Depends(require_user)):
    ctx = trade_context_for(user)
    return {"trades": ctx.paper_live_trades_from_state()}


@router.post("/manual-trade/update-entry")
async def dashboard_manual_trade_update_entry(
    body: ManualEntryUpdateBody,
    user: UserClaims = Depends(require_user),
):
    ctx = trade_context_for(user)
    try:
        updated = await ctx.update_manual_entry_price(body.trade_id, body.entry_price)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "trade": updated}


@router.post("/manual-trade/remove")
async def dashboard_manual_trade_remove(
    body: ManualTradeRemoveBody,
    user: UserClaims = Depends(require_user),
):
    ctx = trade_context_for(user)
    try:
        result = await ctx.dismiss_manual_trade(body.trade_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **result}


@router.get("/weekly-pnl")
async def dashboard_weekly_pnl(
    week_offset: int = 0,
    user: UserClaims = Depends(require_user),
):
    ctx = trade_context_for(user)
    return ctx.weekly_pnl_dict(week_offset)


@router.get("/monthly-pnl")
async def dashboard_monthly_pnl(
    month_offset: int = 0,
    user: UserClaims = Depends(require_user),
):
    ctx = trade_context_for(user)
    return ctx.monthly_pnl_dict(month_offset)


@router.get("/audit-log")
async def dashboard_audit_log(
    user: UserClaims = Depends(require_user),
    max_lines: int = Query(200, ge=1, le=2000),
):
    lines = read_recent_audit_lines(user.username, max_lines=max_lines)
    return {"username": user.username, "lines": lines, "count": len(lines)}
