"""OrderFlowMap UI + Upstox Live WebSocket on ak07.in."""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse

from app.services.ofmap_bridge import (
    handle_ofmap_client,
    ofmap_static_dir,
)

logger = logging.getLogger("ak07.ofmap")

router = APIRouter(tags=["ofmap"])


@router.get("/api/ofmap/health")
async def ofmap_health():
    static = ofmap_static_dir()
    return {
        "ok": True,
        "ui": "/ofmap/",
        "ws": "/api/ofmap/ws",
        "static_exists": (static / "index.html").is_file(),
    }


@router.websocket("/api/ofmap/ws")
async def ofmap_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        await handle_ofmap_client(websocket)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("OFMap WebSocket handler failed")
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/ofmap")
async def ofmap_redirect():
    return RedirectResponse(url="/ofmap/", status_code=307)


@router.get("/ofmap/")
async def ofmap_index():
    path = ofmap_static_dir() / "index.html"
    if not path.is_file():
        return {"error": "OrderFlowMap index.html missing", "path": str(path)}
    return FileResponse(path, media_type="text/html")
