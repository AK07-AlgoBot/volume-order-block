"""FastAPI entrypoint: mount routers and CORS."""

from __future__ import annotations

from app.config.paths import ensure_repo_and_lib_on_path

ensure_repo_and_lib_on_path()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config.settings import get_settings
from app.routes import (
    auth,
    settings_credentials,
    settings_upstox,
)

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
app.include_router(settings_upstox.router)
app.include_router(settings_credentials.router)


@app.get("/api/health")
async def health():
    return {"ok": True}
