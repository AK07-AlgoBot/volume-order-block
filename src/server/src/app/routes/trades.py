from fastapi import APIRouter, Depends, Request

from app.dependencies import trade_context_for_bot
from app.models.schemas import Trade, WeeklyPnlPoint
from app.services.trade_context import TradeUserContext

router = APIRouter(prefix="/api", tags=["trades"])


def _bot_ctx(request: Request) -> TradeUserContext:
    return trade_context_for_bot(request)


@router.post("/trade/open")
async def trade_open(trade: Trade, ctx: TradeUserContext = Depends(_bot_ctx)):
    payload = trade.model_dump()
    ctx.upsert_live_trade(payload)
    await ctx.broadcast({"type": "trade_opened", "trade": payload})
    return {"ok": True}


@router.post("/trade/update")
async def trade_update(trade: Trade, ctx: TradeUserContext = Depends(_bot_ctx)):
    payload = trade.model_dump()
    ctx.upsert_live_trade(payload)
    await ctx.broadcast({"type": "trade_updated", "trade": payload})
    return {"ok": True}


@router.post("/trades/update-batch")
async def trade_update_batch(trades: list[Trade], ctx: TradeUserContext = Depends(_bot_ctx)):
    if not trades:
        return {"ok": True, "updated": 0}
    updated_payloads = []
    for trade in trades:
        payload = trade.model_dump()
        ctx.upsert_live_trade(payload)
        updated_payloads.append(payload)
    await ctx.broadcast({"type": "trades_updated_batch", "trades": updated_payloads})
    return {"ok": True, "updated": len(updated_payloads)}


@router.post("/trade/close")
async def trade_close(trade: Trade, ctx: TradeUserContext = Depends(_bot_ctx)):
    payload = trade.model_dump()
    await ctx.apply_trade_close(payload)
    return {"ok": True}


@router.post("/weekly-pnl")
async def set_weekly_pnl(points: list[WeeklyPnlPoint], ctx: TradeUserContext = Depends(_bot_ctx)):
    _ = [point.model_dump() for point in points]
    computed = ctx._compute_weekly_pnl_from_orders(week_offset=0)
    await ctx.broadcast({"type": "pnl_update", "weekly_pnl": computed})
    return {"ok": True, "points": len(computed), "source": "orders.log"}
