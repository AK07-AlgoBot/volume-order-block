"""Kite Connect OAuth redirect + callback."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from kiteconnect.exceptions import KiteException

from app.config import get_settings, kite_login_url
from app.services.kite_session import exchange_request_token

router = APIRouter(tags=["kite-auth"])


@router.get("/kite/start", summary="Begin Kite login (redirect to Zerodha)")
async def kite_start() -> RedirectResponse:
    settings = get_settings()
    return RedirectResponse(
        url=kite_login_url(settings.kite_api_key),
        status_code=302,
    )


@router.get("/kite/callback", summary="OAuth callback from Zerodha")
async def kite_callback(
    request: Request,
    request_token: str | None = None,
    status: str | None = None,
    action: str | None = None,
) -> HTMLResponse:
    """
    Zerodha redirects here with query params, e.g.:
    ?request_token=...&action=login&status=success
    """
    settings = get_settings()

    if status and status.lower() != "success":
        return HTMLResponse(
            _page(
                "Login not completed",
                f"Zerodha returned status={status!r}. Try again from the home page.",
                ok=False,
            ),
            status_code=400,
        )

    if not request_token:
        return HTMLResponse(
            _page(
                "Missing request_token",
                "No request_token in callback URL. Start login again from / .",
                ok=False,
            ),
            status_code=400,
        )

    try:
        result = exchange_request_token(
            settings.kite_api_key,
            settings.kite_api_secret,
            request_token.strip(),
        )
    except KiteException as e:
        return HTMLResponse(
            _page("Kite API error", str(e), ok=False),
            status_code=502,
        )
    except Exception as e:
        return HTMLResponse(
            _page("Unexpected error", str(e), ok=False),
            status_code=500,
        )

    # Persist session server-side (signed cookie — not the raw secret in the browser)
    request.session["kite_access_token"] = result.access_token
    request.session["kite_user_id"] = result.user_id
    request.session["kite_user_name"] = result.user_name

    return HTMLResponse(
        _page(
            "Authenticated",
            f"Welcome, {result.user_name or result.user_id or 'trader'}. "
            f"User ID: {result.user_id}. Broker: {result.broker}. "
            f"<br><br><a href=\"/me\">View session / profile JSON</a> · "
            f"<a href=\"/logout\">Log out</a>",
            ok=True,
        )
    )


@router.get("/me", summary="Verify session — returns Kite profile JSON")
async def me(request: Request) -> dict:
    token = request.session.get("kite_access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in. Visit / then complete Kite login.")

    settings = get_settings()
    from app.services.kite_session import fetch_profile

    try:
        return fetch_profile(settings.kite_api_key, token)
    except KiteException as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/logout")
@router.get("/logout", summary="Clear session")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


def _page(title: str, body: str, *, ok: bool) -> str:
    color = "#0a0" if ok else "#a00"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;">
<h1 style="color:{color}">{title}</h1>
<p>{body}</p>
<p><a href="/">Home</a></p>
</body></html>"""
