"""Zerodha Kite REST: quote + historical candles."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
KITE_ROOT = "https://api.kite.trade"


def _headers(api_key: str, access_token: str) -> dict[str, str]:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }


def kite_quote(api_key: str, access_token: str, instrument_key: str) -> dict[str, Any] | None:
    """instrument_key e.g. NSE:RELIANCE-EQ. Returns quote object or None."""
    url = f"{KITE_ROOT}/quote?" + urlencode([("i", instrument_key)])
    r = requests.get(url, headers=_headers(api_key, access_token), timeout=45)
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    return data.get(instrument_key)


def normalize_symbol(symbol: str) -> list[str]:
    """Return candidate instrument keys to try on NSE/BSE."""
    s = (symbol or "").strip().upper()
    if not s:
        return []
    if ":" in s:
        return [s]
    base = s.replace(" ", "")
    if base.endswith("-EQ"):
        return [f"NSE:{base}", f"BSE:{base}"]
    return [f"NSE:{base}-EQ", f"NSE:{base}", f"BSE:{base}-EQ", f"BSE:{base}"]


def resolve_instrument(api_key: str, access_token: str, symbol: str) -> tuple[str, int] | None:
    """Return (instrument_key, instrument_token) or None."""
    for key in normalize_symbol(symbol):
        q = kite_quote(api_key, access_token, key)
        if q and q.get("instrument_token") is not None:
            return key, int(q["instrument_token"])
    return None


def fetch_historical(
    api_key: str,
    access_token: str,
    instrument_token: int,
    interval: str,
    from_dt: datetime,
    to_dt: datetime,
    base_url: str | None = None,
) -> list[list[Any]]:
    """Returns raw candle rows from Kite."""
    root = (base_url or KITE_ROOT).rstrip("/")
    from_s = from_dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    to_s = to_dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    path = f"{root}/instruments/historical/{instrument_token}/{interval}"
    qs = urlencode({"from": from_s, "to": to_s, "continuous": "0", "oi": "0"})
    url = f"{path}?{qs}"
    r = requests.get(url, headers=_headers(api_key, access_token), timeout=90)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError(str(payload.get("message") or payload))
    data = payload.get("data") or {}
    candles = data.get("candles") if isinstance(data, dict) else None
    return candles if isinstance(candles, list) else []


def default_fetch_windows(now: datetime | None = None) -> dict[str, tuple[datetime, datetime]]:
    """IST windows for each interval (conservative vs Kite limits)."""
    n = now or datetime.now(IST)
    if n.tzinfo is None:
        n = n.replace(tzinfo=IST)
    else:
        n = n.astimezone(IST)
    return {
        "day": (n - timedelta(days=220), n),
        "60minute": (n - timedelta(days=45), n),
        "30minute": (n - timedelta(days=25), n),
        "15minute": (n - timedelta(days=18), n),
        "5minute": (n - timedelta(days=12), n),
    }
