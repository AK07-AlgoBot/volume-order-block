"""Resolve Zerodha Kite instrument_token for futures from instruments CSV."""

from __future__ import annotations

import csv
import io
import threading
import time
from datetime import date, datetime
from typing import Any

import requests
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
KITE_ROOT = "https://api.kite.trade"

_CACHE_LOCK = threading.Lock()
_CSV_CACHE: dict[str, tuple[float, str]] = {}


def _headers(api_key: str, access_token: str) -> dict[str, str]:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }


def _fetch_csv(exchange: str, api_key: str, access_token: str, ttl_sec: float = 3600.0) -> str:
    ex = (exchange or "NFO").strip().upper()
    now = time.time()
    with _CACHE_LOCK:
        ent = _CSV_CACHE.get(ex)
        if ent and (now - ent[0]) < ttl_sec:
            return ent[1]
    url = f"{KITE_ROOT.rstrip('/')}/instruments/{ex}"
    r = requests.get(url, headers=_headers(api_key, access_token), timeout=120)
    r.raise_for_status()
    text = r.text
    with _CACHE_LOCK:
        _CSV_CACHE[ex] = (now, text)
    return text


def _parse_expiry(row: dict[str, str]) -> date | None:
    raw = (row.get("expiry") or "").strip()
    if not raw:
        return None
    try:
        if len(raw) >= 10:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _underlying_name_for_script(script_name: str) -> str:
    s = str(script_name or "").strip().upper()
    if s == "SENSEX":
        return "SENSEX"
    # MCX short names vs Kite `name` column (align with trading_bot MCX roots)
    mcx = {
        "CRUDE": "CRUDEOIL",
        "GOLDMINI": "GOLDM",
        "SILVERMINI": "SILVERM",
    }
    return mcx.get(s, s)


def kite_exchange_for_script(script_name: str) -> str:
    """Which Kite instruments CSV to load."""
    s = str(script_name or "").strip().upper()
    if s == "SENSEX":
        return "BFO"
    if s in ("CRUDE", "GOLDMINI", "SILVERMINI"):
        return "MCX"
    return "NFO"


def resolve_futures_instrument_token(
    script_name: str,
    api_key: str,
    access_token: str,
) -> int | None:
    """
    Pick nearest active futures contract token (FUT, front expiry >= today IST).
    """
    ex = kite_exchange_for_script(script_name)
    want_name = _underlying_name_for_script(script_name)
    text = _fetch_csv(ex, api_key, access_token)
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    for row in reader:
        if not isinstance(row, dict):
            continue
        it = (row.get("instrument_type") or "").strip().upper()
        if it != "FUT":
            continue
        name = (row.get("name") or "").strip().upper()
        if name != want_name:
            continue
        seg = (row.get("segment") or "").strip().upper()
        if "FUT" not in seg:
            continue
        exp = _parse_expiry(row)
        if exp is None:
            continue
        try:
            tok = int(float(row.get("instrument_token") or 0))
        except (TypeError, ValueError):
            continue
        if tok <= 0:
            continue
        rows.append({"expiry": exp, "token": tok, "tradingsymbol": row.get("tradingsymbol", "")})

    today = datetime.now(IST).date()
    fut_rows = [r for r in rows if r["expiry"] >= today]
    use = fut_rows if fut_rows else rows
    if not use:
        return None
    use.sort(key=lambda r: r["expiry"])
    return int(use[0]["token"])
