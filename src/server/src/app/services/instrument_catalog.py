"""Upstox instrument catalog — local search index for NSE, BSE, and MCX."""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.config.paths import server_root

logger = logging.getLogger("ak07.instrument_catalog")

COMPLETE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
CACHE_DIR = server_root() / "data" / "instruments"
GZ_FILE = CACHE_DIR / "upstox_complete.json.gz"
INDEX_FILE = CACHE_DIR / "search_index.json"
META_FILE = CACHE_DIR / "catalog_meta.json"

INDEX_SEGMENTS = frozenset({"NSE_EQ", "NSE_INDEX", "BSE_EQ", "BSE_INDEX", "MCX_FO"})
DEFAULT_MAX_AGE_HOURS = 24

_index_cache: list[dict] | None = None
_index_mtime: float = 0.0


@dataclass(frozen=True)
class InstrumentPick:
    label: str
    trading_symbol: str
    exchange: str
    segment: str
    instrument_type: str
    instrument_key: str
    name: str


def _include_row(row: dict) -> bool:
    segment = str(row.get("segment") or "")
    if segment not in INDEX_SEGMENTS:
        return False
    inst_type = str(row.get("instrument_type") or "")
    if segment in {"NSE_EQ", "BSE_EQ"}:
        return inst_type == "EQ"
    if segment in {"NSE_INDEX", "BSE_INDEX"}:
        return inst_type == "INDEX"
    if segment == "MCX_FO":
        return inst_type == "FUT"
    return False


def _compact_row(row: dict) -> dict:
    trading_symbol = str(row.get("trading_symbol") or "").strip()
    name = str(row.get("name") or row.get("short_name") or trading_symbol).strip()
    exchange = str(row.get("exchange") or "").strip()
    segment = str(row.get("segment") or "").strip()
    inst_type = str(row.get("instrument_type") or "").strip()
    instrument_key = str(row.get("instrument_key") or "").strip()
    symbol_upper = trading_symbol.upper()
    return {
        "trading_symbol": trading_symbol,
        "symbol_upper": symbol_upper,
        "name": name,
        "name_upper": name.upper(),
        "exchange": exchange,
        "segment": segment,
        "instrument_type": inst_type,
        "instrument_key": instrument_key,
        "expiry": row.get("expiry"),
        "search_key": f"{symbol_upper} {name.upper()} {exchange} {segment}",
    }


def _write_meta(source: str) -> None:
    META_FILE.write_text(
        json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "source": source}, indent=2),
        encoding="utf-8",
    )


def _download_complete_file() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading Upstox instrument master…")
    response = requests.get(COMPLETE_URL, timeout=120)
    response.raise_for_status()
    GZ_FILE.write_bytes(response.content)
    _write_meta(COMPLETE_URL)


def _build_index_file() -> list[dict]:
    with gzip.open(GZ_FILE, "rt", encoding="utf-8") as handle:
        rows = json.load(handle)
    index = [_compact_row(row) for row in rows if _include_row(row)]
    index.sort(key=lambda r: (r["symbol_upper"], r["exchange"], r.get("expiry") or 0))
    INDEX_FILE.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    logger.info("Built instrument search index: %d rows", len(index))
    return index


def ensure_catalog(max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> tuple[bool, str]:
    """Download and index Upstox instruments if cache is missing or stale."""
    global _index_cache, _index_mtime

    try:
        stale = True
        if GZ_FILE.exists() and META_FILE.exists():
            age_hours = (time.time() - GZ_FILE.stat().st_mtime) / 3600
            stale = age_hours > max_age_hours

        if not GZ_FILE.exists() or stale:
            _download_complete_file()

        if not INDEX_FILE.exists() or INDEX_FILE.stat().st_mtime < GZ_FILE.stat().st_mtime:
            _index_cache = _build_index_file()
            _index_mtime = INDEX_FILE.stat().st_mtime
            return True, f"Catalog synced ({len(_index_cache):,} instruments)."

        return True, "Catalog ready."
    except Exception as exc:
        logger.exception("Instrument catalog sync failed")
        return False, f"Catalog sync failed: {exc}"


def catalog_status() -> dict:
    if not META_FILE.exists():
        return {"ready": False, "count": 0, "updated_at": None}
    meta = json.loads(META_FILE.read_text(encoding="utf-8"))
    count = 0
    if INDEX_FILE.exists():
        try:
            count = len(json.loads(INDEX_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            count = 0
    return {"ready": INDEX_FILE.exists(), "count": count, "updated_at": meta.get("updated_at")}


def _load_index() -> list[dict]:
    global _index_cache, _index_mtime
    if not INDEX_FILE.exists():
        ensure_catalog()
    mtime = INDEX_FILE.stat().st_mtime if INDEX_FILE.exists() else 0.0
    if _index_cache is None or mtime != _index_mtime:
        _index_cache = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        _index_mtime = mtime
    return _index_cache


def _pick_label(row: dict) -> str:
    return (
        f"{row['trading_symbol']} · {row['exchange']} · {row['instrument_type']} · "
        f"{row['name'][:48]}"
    )


def search_instruments(query: str, limit: int = 40) -> list[InstrumentPick]:
    """Return instruments matching ``query`` (minimum 3 characters)."""
    q = query.strip().upper()
    if len(q) < 3:
        return []

    try:
        index = _load_index()
    except Exception:
        ensure_catalog()
        index = _load_index()

    prefix_hits: list[dict] = []
    contains_hits: list[dict] = []

    for row in index:
        symbol = row["symbol_upper"]
        if symbol.startswith(q):
            prefix_hits.append(row)
            continue
        if q in row["search_key"]:
            contains_hits.append(row)

    prefix_hits.sort(key=lambda r: (len(r["symbol_upper"]), r["symbol_upper"], r["exchange"]))
    contains_hits.sort(key=lambda r: (r["symbol_upper"], r["exchange"]))

    seen: set[str] = set()
    ordered: list[dict] = []
    for row in prefix_hits + contains_hits:
        key = row["instrument_key"]
        if key in seen:
            continue
        seen.add(key)
        ordered.append(row)
        if len(ordered) >= limit:
            break

    return [
        InstrumentPick(
            label=_pick_label(row),
            trading_symbol=row["trading_symbol"],
            exchange=row["exchange"],
            segment=row["segment"],
            instrument_type=row["instrument_type"],
            instrument_key=row["instrument_key"],
            name=row["name"],
        )
        for row in ordered
    ]


def search_instruments_api(
    query: str,
    access_token: str,
    base_url: str,
    limit: int = 30,
) -> list[InstrumentPick]:
    """Fallback live search via Upstox API when local catalog is unavailable."""
    q = query.strip()
    if len(q) < 3 or not access_token:
        return []
    url = f"{base_url.rstrip('/')}/instruments/search"
    params = {
        "query": q,
        "exchanges": "NSE,BSE,MCX",
        "segments": "EQ,INDEX,COMM,FUT",
        "records": min(limit, 30),
    }
    headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        if response.status_code != 200:
            return []
        rows = response.json().get("data") or []
    except Exception:
        return []

    picks: list[InstrumentPick] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        segment = str(row.get("segment") or "")
        inst_type = str(row.get("instrument_type") or "")
        if segment.endswith("_EQ") and inst_type != "EQ":
            continue
        if segment.endswith("_INDEX") and inst_type != "INDEX":
            continue
        if segment == "MCX_FO" and inst_type != "FUT":
            continue
        compact = _compact_row(row)
        picks.append(
            InstrumentPick(
                label=_pick_label(compact),
                trading_symbol=compact["trading_symbol"],
                exchange=compact["exchange"],
                segment=compact["segment"],
                instrument_type=compact["instrument_type"],
                instrument_key=compact["instrument_key"],
                name=compact["name"],
            )
        )
    return picks


QUICK_PICKS: tuple[InstrumentPick, ...] = (
    InstrumentPick(
        label="NIFTY · NSE · INDEX · Nifty 50",
        trading_symbol="NIFTY",
        exchange="NSE",
        segment="NSE_INDEX",
        instrument_type="INDEX",
        instrument_key="NSE_INDEX|Nifty 50",
        name="Nifty 50",
    ),
    InstrumentPick(
        label="BANKNIFTY · NSE · INDEX · Nifty Bank",
        trading_symbol="BANKNIFTY",
        exchange="NSE",
        segment="NSE_INDEX",
        instrument_type="INDEX",
        instrument_key="NSE_INDEX|Nifty Bank",
        name="Nifty Bank",
    ),
    InstrumentPick(
        label="SENSEX · BSE · INDEX · SENSEX",
        trading_symbol="SENSEX",
        exchange="BSE",
        segment="BSE_INDEX",
        instrument_type="INDEX",
        instrument_key="BSE_INDEX|SENSEX",
        name="SENSEX",
    ),
    InstrumentPick(
        label="RELIANCE · NSE · EQ · RELIANCE INDUSTRIES LTD",
        trading_symbol="RELIANCE",
        exchange="NSE",
        segment="NSE_EQ",
        instrument_type="EQ",
        instrument_key="NSE_EQ|INE002A01018",
        name="RELIANCE INDUSTRIES LTD",
    ),
)
