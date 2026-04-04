"""FastAPI entrypoint: mount routers and CORS."""

from __future__ import annotations

import sys
from pathlib import Path

# Repository root (parent of server/) for `import bot_process_control` / upstox_credentials_store
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config.settings import get_settings
from app.routes import auth, dashboard, settings_trading, settings_upstox, trades, websocket

app = FastAPI(title="AK07 Dashboard API", version="2.0.0")

_s = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_s.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(trades.router)
app.include_router(settings_upstox.router)
app.include_router(settings_trading.router)
app.include_router(websocket.router)


@app.get("/api/health")
async def health():
    return {"ok": True}
