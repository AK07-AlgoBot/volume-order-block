from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException

from app.dependencies import claims_from_token_query, trade_context_for_ws

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/trades")
async def ws_trades(
    websocket: WebSocket,
    token: str | None = Query(None),
):
    await websocket.accept()
    try:
        claims = claims_from_token_query(token)
        ctx = trade_context_for_ws(claims)
    except HTTPException:
        await websocket.close(code=1008)
        return

    ctx.ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in ctx.ws_clients:
            ctx.ws_clients.remove(websocket)
    except Exception:
        if websocket in ctx.ws_clients:
            ctx.ws_clients.remove(websocket)
