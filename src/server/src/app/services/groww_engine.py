"""Groww Trade API — index futures resolve + market orders for S3 fan-out."""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from datetime import date, datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

import requests

from app.config.paths import ensure_repo_and_lib_on_path, server_root

ensure_repo_and_lib_on_path()

from groww_credentials_store import (  # noqa: E402
    DEFAULT_GROWW_BASE_URL,
    groww_auth_header,
    read_credentials_file_for_user,
)
from app.services.upstox_engine import _format_future_contract_label

logger = logging.getLogger("ak07.groww_engine")

IST: Final = ZoneInfo("Asia/Kolkata")
GROWW_CSV_URL: Final = "https://growwapi-assets.groww.in/instruments/instrument.csv"
GROWW_CSV_CACHE: Final = server_root() / "data" / "instruments" / "groww_instrument.csv"

_INDEX_UNDERLYING: Final[dict[str, tuple[str, str]]] = {
    "NIFTY": ("NIFTY", "NSE"),
    "BANKNIFTY": ("BANKNIFTY", "NSE"),
    "SENSEX": ("SENSEX", "BSE"),
}


def _order_reference_id(username: str) -> str:
    """Groww: 8–20 chars, alphanumeric, at most two hyphens (GA001 if violated)."""
    safe = re.sub(r"[^a-zA-Z0-9]", "", username)[:6] or "user"
    ts = int(time.time()) % 10_000_000
    ref = f"ak07s3{safe}{ts}"
    return ref[:20] if len(ref) >= 8 else ref.ljust(8, "0")[:20]


def _ensure_groww_csv() -> bool:
    GROWW_CSV_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if GROWW_CSV_CACHE.exists():
        age_hours = (time.time() - GROWW_CSV_CACHE.stat().st_mtime) / 3600
        if age_hours < 12:
            return True
    try:
        response = requests.get(GROWW_CSV_URL, timeout=120)
        response.raise_for_status()
        GROWW_CSV_CACHE.write_bytes(response.content)
        return True
    except requests.RequestException as exc:
        logger.warning("Groww instrument CSV download failed: %s", exc)
        return GROWW_CSV_CACHE.exists()


def _load_index_futures_from_csv(index_code: str) -> list[dict[str, str]]:
    underlying, exchange = _INDEX_UNDERLYING.get(index_code.upper(), (index_code.upper(), "NSE"))
    if not _ensure_groww_csv():
        return []
    try:
        text = GROWW_CSV_CACHE.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Groww CSV read failed: %s", exc)
        return []

    rows: list[dict[str, str]] = []
    for row in csv.DictReader(io.StringIO(text)):
        if str(row.get("exchange") or "").upper() != exchange:
            continue
        if str(row.get("segment") or "").upper() != "FNO":
            continue
        if str(row.get("underlying_symbol") or "").upper() != underlying:
            continue
        inst = str(row.get("instrument_type") or "").upper()
        if inst in ("CE", "PE"):
            continue
        sym = str(row.get("trading_symbol") or "").upper()
        if underlying == "NIFTY" and ("NXT" in sym or sym.startswith("FINNIFTY") or sym.startswith("MIDCPNIFTY")):
            continue
        expiry = str(row.get("expiry_date") or "")[:10]
        if not expiry:
            continue
        rows.append(
            {
                "trading_symbol": str(row.get("trading_symbol") or "").strip(),
                "groww_symbol": str(row.get("groww_symbol") or "").strip(),
                "expiry_date": expiry,
                "lot_size": str(row.get("lot_size") or "65").strip(),
                "exchange": exchange,
            }
        )
    rows.sort(key=lambda r: r["expiry_date"])
    return rows


def _trading_symbol_for_groww_symbol(groww_symbol: str) -> str:
    if not groww_symbol or not _ensure_groww_csv():
        return groww_symbol
    try:
        text = GROWW_CSV_CACHE.read_text(encoding="utf-8")
    except OSError:
        return groww_symbol
    for row in csv.DictReader(io.StringIO(text)):
        if str(row.get("groww_symbol") or "").strip() == groww_symbol:
            return str(row.get("trading_symbol") or groww_symbol).strip()
    return groww_symbol


class GrowwClient:
    def __init__(self, username: str) -> None:
        self.username = username
        self._creds = read_credentials_file_for_user(username)
        self.base_url = (self._creds.get("base_url") or DEFAULT_GROWW_BASE_URL).rstrip("/")

    def has_token(self) -> bool:
        return bool((self._creds.get("access_token") or "").strip())

    def _headers(self) -> dict[str, str]:
        return {
            **groww_auth_header(self._creds),
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any] | None:
        if not self.has_token():
            return None
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                params=params,
                headers=self._headers(),
                timeout=30,
            )
            if response.status_code != 200:
                logger.warning("Groww GET %s -> HTTP %d %s", path, response.status_code, response.text[:200])
                return None
            payload = response.json()
            if isinstance(payload, dict) and payload.get("status") == "FAILURE":
                logger.warning("Groww GET %s failed: %s", path, payload)
                return None
            if isinstance(payload, dict):
                return payload.get("payload", payload)
            return payload
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Groww GET %s error: %s", path, exc)
            return None

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if not self.has_token():
            logger.warning("[%s] Groww POST skipped — no access token", self.username)
            return None
        try:
            response = requests.post(
                f"{self.base_url}{path}",
                json=body,
                headers=self._headers(),
                timeout=30,
            )
            payload: dict[str, Any] = {}
            try:
                payload = response.json() if response.text else {}
            except ValueError:
                payload = {}
            if response.status_code != 200 or payload.get("status") != "SUCCESS":
                err = payload.get("error") or payload.get("message") or payload.get("remark") or response.text[:300]
                logger.warning(
                    "[%s] Groww POST %s failed HTTP %d — %s",
                    self.username,
                    path,
                    response.status_code,
                    err,
                )
                return None
            data = payload.get("payload")
            return data if isinstance(data, dict) else payload
        except requests.RequestException as exc:
            logger.warning("[%s] Groww POST %s error: %s", self.username, path, exc)
            return None

    def get_fno_positions(self) -> list[dict[str, Any]]:
        """Open FNO positions from Groww portfolio API."""
        payload = self._get("/v1/positions/user", {"segment": "FNO"})
        if not isinstance(payload, dict):
            return []
        rows = payload.get("positions") or []
        if not isinstance(rows, list):
            return []
        open_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qty = int(row.get("quantity") or row.get("net_quantity") or 0)
            if qty == 0:
                cf = int(row.get("net_carry_forward_quantity") or 0)
                if cf == 0:
                    continue
                qty = cf
            open_rows.append(row)
        return open_rows

    def get_index_future_contract(self, index_code: str) -> dict[str, Any] | None:
        """Nearest-expiry index future for Groww order placement."""
        code = index_code.upper()
        today = date.today().isoformat()
        rows = _load_index_futures_from_csv(code)
        candidates = [row for row in rows if row["expiry_date"] >= today]
        if not candidates:
            underlying, exchange = _INDEX_UNDERLYING.get(code, (code, "NSE"))
            expiries_raw = self._get(
                "/v1/historical/expiries",
                {"exchange": exchange, "underlying_symbol": underlying, "year": date.today().year},
            )
            expiries: list[str] = []
            if isinstance(expiries_raw, dict):
                expiries = [str(x) for x in (expiries_raw.get("expiries") or [])]
            elif isinstance(expiries_raw, list):
                expiries = [str(x) for x in expiries_raw]
            expiries = sorted(e for e in expiries if e >= today)
            for exp in expiries:
                contracts_raw = self._get(
                    "/v1/historical/contracts",
                    {"exchange": exchange, "underlying_symbol": underlying, "expiry_date": exp},
                )
                symbols: list[str] = []
                if isinstance(contracts_raw, dict):
                    symbols = [str(x) for x in (contracts_raw.get("contracts") or [])]
                elif isinstance(contracts_raw, list):
                    symbols = [str(x) for x in contracts_raw]
                fut_sym = next(
                    (
                        s
                        for s in symbols
                        if s.endswith("-FUT")
                        and f"-{underlying}-" in s.upper()
                        and "NXT" not in s.upper()
                    ),
                    None,
                )
                if fut_sym:
                    trading_symbol = _trading_symbol_for_groww_symbol(fut_sym)
                    return {
                        "trading_symbol": trading_symbol,
                        "groww_symbol": fut_sym,
                        "expiry": exp[:10],
                        "contract_label": _format_future_contract_label(code, exp[:10]),
                        "lot_size": 65,
                        "instrument_key": f"groww:{fut_sym}",
                    }
            logger.warning("[%s] No Groww %s future found", self.username, code)
            return None

        row = candidates[0]
        return {
            "trading_symbol": row["trading_symbol"],
            "groww_symbol": row["groww_symbol"],
            "expiry": row["expiry_date"],
            "contract_label": _format_future_contract_label(code, row["expiry_date"]),
            "lot_size": int(row.get("lot_size") or 65),
            "instrument_key": f"groww:{row['groww_symbol']}",
        }

    def place_market_order(
        self,
        trading_symbol: str,
        quantity: int,
        transaction_type: str,
        *,
        exchange: str = "NSE",
        segment: str = "FNO",
    ) -> str | None:
        """Place MIS market order. Returns groww_order_id on success."""
        ref = _order_reference_id(self.username)
        body = {
            "trading_symbol": trading_symbol,
            "quantity": int(quantity),
            "validity": "DAY",
            "exchange": exchange,
            "segment": segment,
            "product": "MIS",
            "order_type": "MARKET",
            "transaction_type": transaction_type.upper(),
            "order_reference_id": ref,
        }
        data = self._post("/v1/order/create", body)
        if not data:
            return None
        order_id = str(data.get("groww_order_id") or "")
        if order_id:
            logger.info(
                "[%s] Groww order OK: %s %d x %s (%s)",
                self.username,
                transaction_type.upper(),
                quantity,
                trading_symbol,
                order_id,
            )
        return order_id or None
