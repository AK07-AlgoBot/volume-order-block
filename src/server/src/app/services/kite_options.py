"""Zerodha Kite: index / options helpers — ATM strike from spot + NFO instrument master."""

from __future__ import annotations

import csv
import io
import math
import threading
import time
from datetime import date, datetime
from typing import Any, Sequence

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import requests

from app.config.paths import server_root
from app.services.kite_historical import KITE_ROOT, kite_quote

_CACHE_LOCK = threading.Lock()
_NFO_CSV_TEXT: str | None = None
_NFO_LOADED_AT: float = 0.0
NFO_MEMORY_TTL_SEC = 6 * 3600

_CACHE_DIR = server_root() / "data" / "cache"
_NFO_CACHE_FILE = _CACHE_DIR / "kite_nfo_instruments.csv"
FILE_MAX_AGE_SEC = 7 * 24 * 3600

# Kite quote keys for spot (LTP / last_price in quote payload)
INDEX_INSTRUMENT: dict[str, str] = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
}

# Same universe as INDEX_INSTRUMENT — 33 EMA band / VWAP / RSI / ADX backtests run for each.
INDEX_BAND_STRATEGY_SCRIPTS: tuple[str, ...] = tuple(INDEX_INSTRUMENT.keys())

# `name` column in NFO master for these underlyings
NFO_NAME: dict[str, str] = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
}


def _kite_headers(api_key: str, access_token: str) -> dict[str, str]:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }


def _fetch_nfo_csv_live(api_key: str, access_token: str) -> str:
    url = f"{KITE_ROOT.rstrip('/')}/instruments/NFO"
    r = requests.get(url, headers=_kite_headers(api_key, access_token), timeout=180)
    r.raise_for_status()
    return r.text


def get_nfo_master_csv(api_key: str, access_token: str) -> tuple[str, dict[str, Any]]:
    """Return raw NFO CSV text plus meta about cache source."""
    global _NFO_CSV_TEXT, _NFO_LOADED_AT
    meta: dict[str, Any] = {"source": "unknown"}

    with _CACHE_LOCK:
        now = time.time()
        if _NFO_CSV_TEXT and (now - _NFO_LOADED_AT) < NFO_MEMORY_TTL_SEC:
            meta.update({"source": "memory", "bytes": len(_NFO_CSV_TEXT.encode("utf-8"))})
            return _NFO_CSV_TEXT, meta

        file_age: float | None = None
        if _NFO_CACHE_FILE.is_file():
            try:
                file_age = now - _NFO_CACHE_FILE.stat().st_mtime
            except OSError:
                file_age = None
            if file_age is not None and file_age < FILE_MAX_AGE_SEC:
                text = _NFO_CACHE_FILE.read_text(encoding="utf-8", errors="replace")
                _NFO_CSV_TEXT = text
                _NFO_LOADED_AT = now
                meta.update(
                    {
                        "source": "file_cache",
                        "bytes": len(text.encode("utf-8")),
                        "cache_age_hours": round(file_age / 3600, 2),
                    }
                )
                return text, meta

        text = _fetch_nfo_csv_live(api_key, access_token)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _NFO_CACHE_FILE.write_text(text, encoding="utf-8")
        _NFO_CSV_TEXT = text
        _NFO_LOADED_AT = now
        meta.update({"source": "kite_live", "bytes": len(text.encode("utf-8"))})
        return text, meta


def parse_option_rows_for_underlying(csv_text: str, underlying_key: str) -> list[dict[str, Any]]:
    """Return option rows (CE/PE) where name matches NFO master name for this underlying."""
    want_name = NFO_NAME.get(underlying_key.upper(), underlying_key.upper())
    out: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if (row.get("segment") or "").strip() != "NFO-OPT":
            continue
        it = (row.get("instrument_type") or "").strip()
        if it not in ("CE", "PE"):
            continue
        if (row.get("name") or "").strip() != want_name:
            continue
        try:
            strike = float(row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        exp = (row.get("expiry") or "").strip()
        if not exp:
            continue
        ts = (row.get("tradingsymbol") or "").strip()
        if not ts:
            continue
        try:
            tok = int(float(row.get("instrument_token") or 0))
        except (TypeError, ValueError):
            continue
        if not tok:
            continue
        try:
            lot_sz = int(float(row.get("lot_size") or 0))
        except (TypeError, ValueError):
            lot_sz = 0
        out.append(
            {
                "tradingsymbol": ts,
                "instrument_token": tok,
                "strike": strike,
                "expiry": exp,
                "instrument_type": it,
                "name": want_name,
                "lot_size": lot_sz,
            }
        )
    return out


def list_sorted_expiries(rows: list[dict[str, Any]]) -> list[str]:
    exps = sorted({r["expiry"] for r in rows})
    return exps


def pick_nearest_expiry_on_or_after(expiries: Sequence[str], as_of: date) -> str | None:
    """Expiries are YYYY-MM-DD strings; pick the earliest expiry date >= as_of."""
    best: tuple[date, str] | None = None
    for e in expiries:
        try:
            y, m, d = (int(x) for x in e.split("-")[:3])
            ed = date(y, m, d)
        except (ValueError, TypeError):
            continue
        if ed < as_of:
            continue
        if best is None or ed < best[0]:
            best = (ed, e)
    return best[1] if best else None


def strikes_for_expiry(rows: list[dict[str, Any]], expiry: str) -> list[float]:
    exp = expiry.strip()
    strikes = {r["strike"] for r in rows if r["expiry"] == exp and r["instrument_type"] == "CE"}
    return sorted(strikes)


def atm_strike_nearest(spot: float, strikes: Sequence[float]) -> float:
    """Strike with minimum distance to spot; ties broken by lower strike."""
    if not strikes:
        raise ValueError("strikes is empty")
    return min(strikes, key=lambda k: (abs(float(k) - float(spot)), k))


def find_option_row(
    rows: list[dict[str, Any]],
    expiry: str,
    strike: float,
    instrument_type: str,
) -> dict[str, Any] | None:
    """Match CE/PE row for expiry + strike (exact float match after normalizing)."""
    exp = expiry.strip()
    want = instrument_type.strip().upper()
    for r in rows:
        if r["expiry"] != exp or r["instrument_type"] != want:
            continue
        if math.isclose(float(r["strike"]), float(strike), rel_tol=0, abs_tol=1e-3):
            return r
    return None


def index_spot_ltp(api_key: str, access_token: str, underlying_key: str) -> tuple[float, str]:
    """
    Last traded price for the index from /quote.
    Returns (ltp, instrument_key_used).
    """
    u = underlying_key.upper()
    ikey = INDEX_INSTRUMENT.get(u)
    if not ikey:
        raise ValueError(f"Unknown underlying {underlying_key!r}; try {sorted(INDEX_INSTRUMENT)}")
    q = kite_quote(api_key, access_token, ikey)
    if not q:
        raise RuntimeError(f"No quote for {ikey}")
    for k in ("last_price", "average_price", "ohlc", "oi"):
        if k == "ohlc":
            o = q.get("ohlc")
            if isinstance(o, dict) and o.get("close") is not None:
                return float(o["close"]), ikey
            continue
        v = q.get(k)
        if v is not None:
            try:
                return float(v), ikey
            except (TypeError, ValueError):
                continue
    raise RuntimeError(f"Quote for {ikey} had no usable price fields: {list(q.keys())}")


def atm_detail(
    api_key: str,
    access_token: str,
    underlying_key: str,
    expiry: str | None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """
    Resolve ATM strike: NFO master + index LTP.

    If expiry is None, chooses the nearest expiry date on or after as_of (IST calendar day).
    """
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    if as_of is None:
        as_of = __import__("datetime").datetime.now(ist).date()

    u = underlying_key.upper()
    csv_text, meta = get_nfo_master_csv(api_key, access_token)
    rows = parse_option_rows_for_underlying(csv_text, u)
    exps = list_sorted_expiries(rows)
    chosen = (expiry or "").strip() or pick_nearest_expiry_on_or_after(exps, as_of)
    if not chosen:
        raise RuntimeError(f"No expiries on/after {as_of} for {u} (found {len(exps)} total expiries)")

    strikes = strikes_for_expiry(rows, chosen)
    if not strikes:
        raise RuntimeError(f"No strikes for {u} expiry={chosen}")

    spot, quote_key = index_spot_ltp(api_key, access_token, u)
    atm = atm_strike_nearest(spot, strikes)

    ce_row = next(
        (r for r in rows if r["expiry"] == chosen and r["strike"] == atm and r["instrument_type"] == "CE"),
        None,
    )
    pe_row = next(
        (r for r in rows if r["expiry"] == chosen and r["strike"] == atm and r["instrument_type"] == "PE"),
        None,
    )

    return {
        "underlying": u,
        "index_quote_key": quote_key,
        "spot": spot,
        "expiry": chosen,
        "expiry_picked_automatically": not (expiry or "").strip(),
        "atm_strike": atm,
        "distance_to_spot": round(atm - spot, 4),
        "strike_step_guess": _guess_step(strikes),
        "ce_tradingsymbol": ce_row["tradingsymbol"] if ce_row else None,
        "pe_tradingsymbol": pe_row["tradingsymbol"] if pe_row else None,
        "instrument_master_meta": meta,
        "strikes_at_expiry_count": len(strikes),
        "strikes_min": strikes[0] if strikes else None,
        "strikes_max": strikes[-1] if strikes else None,
    }


def _guess_step(strikes: Sequence[float]) -> float | None:
    if len(strikes) < 2:
        return None
    diffs = [round(strikes[i] - strikes[i - 1], 8) for i in range(1, min(40, len(strikes)))]
    return min(diffs) if diffs else None
