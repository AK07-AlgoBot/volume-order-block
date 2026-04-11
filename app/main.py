"""
AK07 — Zerodha Kite Connect authentication API.

Run: uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import kite_auth
from app.config import get_settings

app = FastAPI(
    title="AK07 Kite Auth",
    description="Minimal Kite Connect OAuth2-style login for Zerodha.",
    version="1.0.0",
)

settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="ak07_kite_session",
    max_age=86400 * 7,  # 7 days
    same_site="lax",
    https_only=False,  # local http://127.0.0.1
)

app.include_router(kite_auth.router)


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AK07 Kite Auth</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;">
<h1>AK07 — Kite Connect</h1>
<p>Log in with your Zerodha account to obtain an API session.</p>
<p><a href="/kite/start" style="display:inline-block;padding:0.6rem 1rem;background:#387ed1;
color:white;text-decoration:none;border-radius:6px;">Login with Zerodha</a></p>
<p style="color:#666;font-size:0.9rem;">After login you will return to this app at the configured redirect URL.</p>
<hr>
<p><a href="/me">/me</a> — JSON profile (requires login)<br>
<a href="/docs">/docs</a> — OpenAPI</p>
</body></html>"""


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Mount callback at path matching KITE_REDIRECT_URL path component
# Router already uses /kite/callback — ensure .env KITE_REDIRECT_URL ends with /kite/callback
