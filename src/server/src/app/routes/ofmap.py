"""OrderFlowMap UI + Upstox Live WebSocket on ak07.in."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.services.ofmap_bridge import (
    handle_ofmap_client,
    ofmap_static_dir,
)

logger = logging.getLogger("ak07.ofmap")

router = APIRouter(tags=["ofmap"])


def _index_response() -> Response:
    path = ofmap_static_dir() / "index.html"
    if not path.is_file():
        body = (
            '{"error":"OrderFlowMap index.html missing","path":'
            + json.dumps(path.as_posix())
            + "}"
        )
        return Response(
            content=body,
            status_code=404,
            media_type="application/json",
        )
    return FileResponse(path, media_type="text/html")


@router.get("/api/ofmap/health")
async def ofmap_health():
    static = ofmap_static_dir()
    return {
        "ok": True,
        "ui": "/api/ofmap/",
        "ui_alt": "/ofmap/",
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


# UI under /api/ofmap/ — works with existing nginx location /api/ (no extra nginx block).
@router.get("/api/ofmap")
async def ofmap_api_redirect():
    return RedirectResponse(url="/api/ofmap/", status_code=307)


@router.get("/api/ofmap/")
async def ofmap_api_index():
    return _index_response()


# Also keep /ofmap/ once host nginx proxies it (see host-nginx-ak07.conf.example).
@router.get("/ofmap")
async def ofmap_redirect():
    return RedirectResponse(url="/api/ofmap/", status_code=307)


@router.get("/ofmap/")
async def ofmap_index():
    return _index_response()
