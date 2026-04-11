"""
Kite Connect instrument master (NSE/BSE EQ) for symbol search / autocomplete.

Uses GET /instruments/:exchange (CSV). Cached on disk to avoid refetching every keystroke.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from typing import Any

import requests

from app.config.paths import server_root
from app.services.kite_historical import KITE_ROOT


def _kite_headers(api_key: str, access_token: str) -> dict[str, str]:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }

_CACHE_LOCK = threading.Lock()
_MEMORY_ROWS: list[dict[str, Any]] | None = None
_MEMORY_LOADED_AT: float = 0.0

# Refresh from Kite if memory cache older than this (seconds)
MEMORY_TTL_SEC = 6 * 3600
# Use file cache if younger than this without hitting API
FILE_MAX_AGE_SEC = 7 * 24 * 3600

_CACHE_DIR = server_root() / "data" / "cache"
_CACHE_FILE = _CACHE_DIR / "kite_equity_instruments.json"


def _fetch_exchange_csv(api_key: str, access_token: str, exchange: str) -> str:
    url = f"{KITE_ROOT.rstrip('/')}/instruments/{exchange}"
    r = requests.get(url, headers=_kite_headers(api_key, access_token), timeout=120)
    r.raise_for_status()
    return r.text


def _parse_eq_rows(csv_text: str, exchange: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if (row.get("instrument_type") or "").strip() != "EQ":
            continue
        ts = (row.get("tradingsymbol") or "").strip()
        if not ts:
            continue
        try:
            tok = int(float(row.get("instrument_token") or 0))
        except (TypeError, ValueError):
            continue
        ex = (row.get("exchange") or exchange).strip()
        out.append(
            {
                "exchange": ex,
                "tradingsymbol": ts,
                "name": (row.get("name") or "").strip(),
                "instrument_token": tok,
                "instrument_key": f"{ex}:{ts}",
            }
        )
    return out


def _download_universe(api_key: str, access_token: str) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for ex in ("NSE", "BSE"):
        text = _fetch_exchange_csv(api_key, access_token, ex)
        merged.extend(_parse_eq_rows(text, ex))
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(merged), encoding="utf-8")
    return merged


def _load_file_cache() -> list[dict[str, Any]] | None:
    if not _CACHE_FILE.is_file():
        return None
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return raw if isinstance(raw, list) else None


def get_equity_universe(api_key: str | None, access_token: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Return (rows, meta). May use memory, file, or live Kite download.
    """
    global _MEMORY_ROWS, _MEMORY_LOADED_AT
    meta: dict[str, Any] = {"source": "unknown", "count": 0}

    with _CACHE_LOCK:
        now = time.time()
        if _MEMORY_ROWS and (now - _MEMORY_LOADED_AT) < MEMORY_TTL_SEC:
            meta.update({"source": "memory", "count": len(_MEMORY_ROWS)})
            return _MEMORY_ROWS, meta

        file_rows = _load_file_cache()
        file_age_sec: float | None = None
        if _CACHE_FILE.is_file():
            try:
                file_age_sec = now - _CACHE_FILE.stat().st_mtime
            except OSError:
                file_age_sec = None

        if file_rows and file_age_sec is not None and file_age_sec < FILE_MAX_AGE_SEC:
            _MEMORY_ROWS = file_rows
            _MEMORY_LOADED_AT = now
            meta.update(
                {
                    "source": "file_cache",
                    "count": len(file_rows),
                    "cache_age_hours": round(file_age_sec / 3600, 2),
                }
            )
            return file_rows, meta

        key = (api_key or "").strip()
        tok = (access_token or "").strip()
        if not key or not tok:
            if file_rows:
                _MEMORY_ROWS = file_rows
                _MEMORY_LOADED_AT = now
                meta.update(
                    {
                        "source": "file_cache_stale",
                        "count": len(file_rows),
                        "warning": "Using on-disk list; connect Zerodha to refresh from Kite.",
                    }
                )
                return file_rows, meta
            raise ValueError(
                "No instrument list yet. Save Zerodha API credentials (Connect with Zerodha) "
                "so the server can download the Kite instrument master once."
            )

        rows = _download_universe(key, tok)
        _MEMORY_ROWS = rows
        _MEMORY_LOADED_AT = now
        meta.update({"source": "kite_live", "count": len(rows)})
        return rows, meta


def search_equities(rows: list[dict[str, Any]], query: str, limit: int = 25) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []

    limit = max(1, min(50, limit))
    scored: list[tuple[int, dict[str, Any]]] = []

    for r in rows:
        ts = (r.get("tradingsymbol") or "").lower()
        nm = (r.get("name") or "").lower()
        if q not in ts and q not in nm:
            continue
        score = 0
        if ts.startswith(q):
            score += 120
        elif q in ts:
            score += 60
        if nm.startswith(q):
            score += 40
        elif q in nm:
            score += 15
        # prefer shorter symbols when tied
        score -= min(len(ts), 30)
        scored.append((-score, r))

    scored.sort(key=lambda x: x[0])
    return [r for _, r in scored[:limit]]
