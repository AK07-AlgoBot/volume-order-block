"""FastAPI entrypoint: mount routers and CORS."""

from __future__ import annotations

from app.config.paths import ensure_repo_and_lib_on_path

ensure_repo_and_lib_on_path()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config.settings import get_settings
from app.routes import (
    admin_users,
    auth,
    groww_token,
    kite_oauth,
    settings_credentials,
    settings_upstox,
    upstox_notifier,
    upstox_oauth,
)

app = FastAPI(title="AK07 Dashboard API", version="2.0.0")


@app.on_event("startup")
async def _startup_seed_users() -> None:
    from app.services.users_store import ensure_seeded_users

    ensure_seeded_users()

_s = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_s.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(admin_users.router)
app.include_router(kite_oauth.router)
app.include_router(upstox_oauth.router)
app.include_router(groww_token.router)
app.include_router(settings_upstox.router)
app.include_router(settings_credentials.router)
app.include_router(upstox_notifier.router)


@app.get("/api/health")
async def health():
    return {"ok": True}
