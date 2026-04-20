"""Resolve Zerodha Kite instrument_token for futures or equity from instruments CSV."""

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


def resolve_equity_instrument_token(
    script_name: str,
    api_key: str,
    access_token: str,
) -> int | None:
    """
    Resolve NSE/BSE cash equity token (segment EQ) by tradingsymbol/name.
    """
    sym = str(script_name or "").strip().upper()
    if not sym:
        return None
    for ex in ("NSE", "BSE"):
        text = _fetch_csv(ex, api_key, access_token)
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            if not isinstance(row, dict):
                continue
            seg = (row.get("segment") or "").strip().upper()
            if not seg.endswith("-EQ"):
                continue
            ts = (row.get("tradingsymbol") or "").strip().upper()
            nm = (row.get("name") or "").strip().upper()
            if sym not in (ts, nm):
                continue
            try:
                tok = int(float(row.get("instrument_token") or 0))
            except (TypeError, ValueError):
                continue
            if tok > 0:
                return tok
    return None


def _norm_symbol(s: str) -> str:
    return "".join((s or "").strip().upper().split())


# Strategy script keys → (Kite exchange CSV, index tradingsymbol as in instruments list).
_INDEX_INSTRUMENT: dict[str, tuple[str, str]] = {
    "NIFTY": ("NSE", "NIFTY 50"),
    "NIFTY50": ("NSE", "NIFTY 50"),
    "BANKNIFTY": ("NSE", "NIFTY BANK"),
    "SENSEX": ("BSE", "SENSEX"),
}


def resolve_nse_index_instrument_token(
    script_name: str,
    api_key: str,
    access_token: str,
) -> int | None:
    """
    Resolve NSE/BSE **spot index** instrument_token (INDEX in *-INDICES segment).

    Use for backtests on the cash index (e.g. NIFTY 50) instead of futures.
    """
    key = _norm_symbol(str(script_name or ""))
    if not key:
        return None
    pair = _INDEX_INSTRUMENT.get(key)
    if not pair:
        return None
    exchange, want_ts = pair
    want_norm = _norm_symbol(want_ts)
    text = _fetch_csv(exchange, api_key, access_token)
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        if not isinstance(row, dict):
            continue
        seg = (row.get("segment") or "").strip().upper()
        # Kite uses segment INDICES with instrument_type EQ (not INDEX) for spot indices.
        if seg != "INDICES":
            continue
        ts = (row.get("tradingsymbol") or "").strip()
        if _norm_symbol(ts) != want_norm:
            continue
        try:
            tok = int(float(row.get("instrument_token") or 0))
        except (TypeError, ValueError):
            continue
        if tok > 0:
            return tok
    return None


def resolve_kite_instrument_token(
    script_name: str,
    api_key: str,
    access_token: str,
) -> int | None:
    """
    Resolve correct token for strategy symbol:
    - Index/MCX strategy symbols => nearest active futures token.
    - Other symbols => cash equity token (fallback to futures if needed).
    """
    s = str(script_name or "").strip().upper()
    force_fut = {"NIFTY", "BANKNIFTY", "SENSEX", "CRUDE", "GOLDMINI", "SILVERMINI"}
    if s in force_fut:
        return resolve_futures_instrument_token(s, api_key, access_token)
    tok_eq = resolve_equity_instrument_token(s, api_key, access_token)
    if tok_eq and tok_eq > 0:
        return tok_eq
    return resolve_futures_instrument_token(s, api_key, access_token)
