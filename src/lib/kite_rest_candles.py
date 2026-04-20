"""Kite Connect REST: historical candles → pandas (for trading bot market data)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import requests
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
KITE_ROOT = "https://api.kite.trade"


def _headers(api_key: str, access_token: str) -> dict[str, str]:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }


def map_bot_interval_to_kite(bot_interval: str) -> str:
    """Map TRADING_CONFIG interval strings to Kite historical interval names."""
    b = (bot_interval or "1minute").strip().lower()
    if b in ("1minute", "minute"):
        return "minute"
    if b == "3minute":
        return "3minute"
    if b == "5minute":
        return "5minute"
    if b == "15minute":
        return "15minute"
    if b == "30minute":
        return "30minute"
    if b == "60minute":
        return "60minute"
    if b == "day":
        return "day"
    return "minute"


def fetch_historical_raw(
    api_key: str,
    access_token: str,
    instrument_token: int,
    kite_interval: str,
    from_dt: datetime,
    to_dt: datetime,
    continuous: str = "0",
    oi: str = "0",
) -> list[list[Any]]:
    root = KITE_ROOT.rstrip("/")
    from_s = from_dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    to_s = to_dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    path = f"{root}/instruments/historical/{instrument_token}/{kite_interval}"
    qs = urlencode({"from": from_s, "to": to_s, "continuous": continuous, "oi": oi})
    url = f"{path}?{qs}"
    r = requests.get(url, headers=_headers(api_key, access_token), timeout=90)
    if not r.ok:
        body = (r.text or "")[:1200].strip()
        raise requests.HTTPError(
            f"{r.status_code} Client Error for historical candles "
            f"(instrument_token={instrument_token}, interval={kite_interval}). "
            f"Kite response: {body or '(empty body)'}",
            response=r,
        )
    payload = r.json()
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError(str(payload.get("message") or payload))
    data = payload.get("data") or {}
    candles = data.get("candles") if isinstance(data, dict) else None
    return candles if isinstance(candles, list) else []


def kite_candles_to_dataframe(rows: list[list[Any]]) -> pd.DataFrame | None:
    """Kite candle row: [timestamp, open, high, low, close, volume, (optional) oi]."""
    if not rows:
        return None
    # Kite can return either 6 fields (without OI) or 7 fields (with OI).
    normalized: list[list[Any]] = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 6:
            continue
        vals = list(r[:7])
        if len(vals) == 6:
            vals.append(None)
        normalized.append(vals)
    if not normalized:
        return None
    df = pd.DataFrame(
        normalized,
        columns=["timestamp", "open", "high", "low", "close", "volume", "oi"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for c in ("open", "high", "low", "close", "volume", "oi"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def default_intraday_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    n = now or datetime.now(IST)
    if n.tzinfo is None:
        n = n.replace(tzinfo=IST)
    else:
        n = n.astimezone(IST)
    return n - timedelta(days=5), n


def default_swing_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    n = now or datetime.now(IST)
    if n.tzinfo is None:
        n = n.replace(tzinfo=IST)
    else:
        n = n.astimezone(IST)
    return n - timedelta(days=7), n
