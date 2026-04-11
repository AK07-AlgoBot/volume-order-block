"""Multi-timeframe swing / order-block analysis via Zerodha Kite historical candles."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException, Query

from app.config.paths import ensure_repo_and_lib_on_path
from app.dependencies import UserClaims, require_user
from app.models.schemas import OrderBlockBody
from app.services.kite_historical import (
    default_fetch_windows,
    fetch_historical,
    resolve_instrument,
)
from app.services.kite_instruments import get_equity_universe, search_equities
from order_block_logic import analyze_pack, rows_to_candles

logger = logging.getLogger(__name__)


def _zerodha_creds_for_user(username: str) -> tuple[str, str, str]:
    from upstox_credentials_store import sanitize_username
    from zerodha_credentials_store import read_credentials_file_for_user

    safe = sanitize_username(username)
    creds = read_credentials_file_for_user(safe)
    api_key = (creds.get("api_key") or "").strip()
    access_token = (creds.get("access_token") or "").strip()
    base_url = (creds.get("base_url") or "").strip() or "https://api.kite.trade"
    return api_key, access_token, base_url

router = APIRouter(prefix="/api/market", tags=["market"])


def _run_kite_analysis(
    api_key: str,
    access_token: str,
    base_url: str,
    symbol: str,
) -> dict[str, Any]:
    ensure_repo_and_lib_on_path()
    resolved = resolve_instrument(api_key, access_token, symbol)
    if not resolved:
        raise ValueError(f"Could not resolve symbol {symbol!r} (try NSE:SYMBOL-EQ).")

    inst_key, inst_token = resolved
    wins = default_fetch_windows()

    def load(iv: str) -> list[list[Any]]:
        a, b = wins[iv]
        return fetch_historical(api_key, access_token, inst_token, iv, a, b, base_url)

    rows_d = load("day")
    rows_60 = load("60minute")
    rows_30 = load("30minute")
    rows_15 = load("15minute")
    rows_5 = load("5minute")

    daily = rows_to_candles(rows_d)
    h60 = rows_to_candles(rows_60)
    m30 = rows_to_candles(rows_30)
    m15 = rows_to_candles(rows_15)
    m5 = rows_to_candles(rows_5)

    if len(daily) < 30:
        raise ValueError("Not enough daily history from Kite for this symbol.")

    analysis = analyze_pack(daily, h60, m30, m15, m5)
    analysis["instrument_key"] = inst_key
    analysis["instrument_token"] = inst_token
    return analysis


@router.get("/instruments/search")
async def instruments_search(
    q: str = "",
    limit: int = Query(25, ge=1, le=50),
    user: UserClaims = Depends(require_user),
):
    """Autocomplete: match NSE/BSE equity symbols and names (Kite instrument master)."""
    ensure_repo_and_lib_on_path()
    api_key, access_token, _base = _zerodha_creds_for_user(user.username)
    try:
        rows, meta = await asyncio.to_thread(get_equity_universe, api_key or None, access_token or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except requests.HTTPError as e:
        logger.warning("Kite instruments HTTP error: %s", e)
        raise HTTPException(status_code=502, detail=f"Kite instrument list failed: {e}") from e
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    results = search_equities(rows, q, limit)
    return {"ok": True, "query": q.strip(), "results": results, "meta": meta}


@router.post("/order-block")
async def order_block(body: OrderBlockBody, user: UserClaims = Depends(require_user)):
    ensure_repo_and_lib_on_path()
    api_key, access_token, base_url = _zerodha_creds_for_user(user.username)

    if not api_key or not access_token:
        raise HTTPException(
            status_code=400,
            detail="Zerodha credentials missing. Connect with Kite OAuth or save API key and access token.",
        )

    sym = body.symbol.strip()
    try:
        payload = await asyncio.to_thread(_run_kite_analysis, api_key, access_token, base_url, sym)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except requests.HTTPError as e:
        logger.warning("Kite HTTP error: %s", e)
        raise HTTPException(status_code=502, detail=f"Kite API error: {e}") from e
    except requests.RequestException as e:
        logger.warning("Kite request failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Kite connectivity: {e}") from e

    return {"ok": True, "symbol": sym, "analysis": payload}
