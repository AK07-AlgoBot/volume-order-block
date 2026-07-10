"""Groww Trade API — index futures resolve + market orders for S3 fan-out."""

from __future__ import annotations

import csv
import io
import logging
import os
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
# Groww MARKET FNO orders can be rejected by RMS LPP rules; LIMIT at live LTP avoids breach.
GROWW_FNO_ORDER_TYPE: Final[str] = (os.environ.get("GROWW_FNO_ORDER_TYPE") or "LIMIT").strip().upper()
GROWW_LIMIT_SLIPPAGE_PTS: Final[float] = float(os.environ.get("GROWW_LIMIT_SLIPPAGE_PTS", "2"))
GROWW_ORDER_STATUS_POLLS: Final[int] = int(os.environ.get("GROWW_ORDER_STATUS_POLLS", "12"))
GROWW_ORDER_STATUS_POLL_SEC: Final[float] = float(os.environ.get("GROWW_ORDER_STATUS_POLL_SEC", "1"))
_FILL_STATUSES: Final[frozenset[str]] = frozenset(
    {"EXECUTED", "COMPLETED", "COMPLETE", "TRADED", "PARTIALLY_EXECUTED"}
)
_OPEN_ORDER_STATUSES: Final[frozenset[str]] = frozenset({"OPEN", "NEW", "ACKED", "APPROVED", "TRIGGER_PENDING"})
_CANCELLABLE_ORDER_STATUSES: Final[frozenset[str]] = frozenset({"OPEN", "NEW", "ACKED"})
_IN_FLIGHT_ORDER_STATUSES: Final[frozenset[str]] = frozenset({"APPROVED", "TRIGGER_PENDING"})
_REJECT_STATUSES: Final[frozenset[str]] = frozenset({"REJECTED", "FAILED", "CANCELLED"})


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

    def net_fno_quantity(self, trading_symbol: str) -> int:
        """Signed net qty for a futures symbol (negative = short)."""
        sym = trading_symbol.upper()
        for row in self.get_fno_positions():
            if str(row.get("trading_symbol") or "").upper() != sym:
                continue
            for key in ("net_quantity", "quantity", "net_qty"):
                if row.get(key) is not None:
                    try:
                        return int(row[key])
                    except (TypeError, ValueError):
                        continue
        return 0

    def has_directional_exposure(
        self,
        trading_symbol: str,
        direction: str,
        min_qty: int = 1,
    ) -> bool:
        qty = self.net_fno_quantity(trading_symbol)
        if direction == "LONG":
            return qty >= min_qty
        return qty <= -min_qty

    def cancel_order(self, groww_order_id: str, *, segment: str = "FNO") -> bool:
        data = self._post(
            "/v1/order/cancel",
            {"groww_order_id": groww_order_id, "segment": segment},
        )
        return bool(data)

    def get_fno_order_list(self) -> list[dict[str, Any]]:
        payload = self._get("/v1/order/list", {"segment": "FNO", "page": 0, "page_size": 100})
        if not isinstance(payload, dict):
            return []
        rows = payload.get("order_list") or []
        return [row for row in rows if isinstance(row, dict)]

    def get_fno_day_pnl(self) -> dict[str, float] | None:
        """Today's FNO P&L from executed orders + open position realised fields."""
        if not self.has_token():
            return None
        today = datetime.now(IST).date().isoformat()
        buckets: dict[str, dict[str, float | int]] = {}
        for row in self.get_fno_order_list():
            stamp = " ".join(
                str(row.get(key) or "")
                for key in ("created_at", "trade_date", "exchange_time")
            )
            if today not in stamp:
                continue
            status = str(row.get("order_status") or "").upper()
            if status not in ("EXECUTED", "COMPLETE", "COMPLETED", "TRADED"):
                continue
            filled = int(row.get("filled_quantity") or 0)
            if filled <= 0:
                continue
            sym = str(row.get("trading_symbol") or "")
            side = str(row.get("transaction_type") or "").upper()
            avg = float(row.get("average_fill_price") or 0)
            bucket = buckets.setdefault(sym, {"buy_v": 0.0, "buy_q": 0, "sell_v": 0.0, "sell_q": 0})
            if side == "BUY":
                bucket["buy_v"] = float(bucket["buy_v"]) + filled * avg
                bucket["buy_q"] = int(bucket["buy_q"]) + filled
            elif side == "SELL":
                bucket["sell_v"] = float(bucket["sell_v"]) + filled * avg
                bucket["sell_q"] = int(bucket["sell_q"]) + filled

        realised = 0.0
        open_positions = 0
        for bucket in buckets.values():
            buy_q = int(bucket["buy_q"])
            sell_q = int(bucket["sell_q"])
            closed = min(buy_q, sell_q)
            if closed > 0 and buy_q > 0 and sell_q > 0:
                avg_buy = float(bucket["buy_v"]) / buy_q
                avg_sell = float(bucket["sell_v"]) / sell_q
                realised += closed * (avg_sell - avg_buy)
            if buy_q != sell_q:
                open_positions += 1

        for row in self.get_fno_positions():
            realised += float(row.get("realised_pnl") or 0)

        return {
            "total_pnl": realised,
            "realised": realised,
            "unrealised": 0.0,
            "open_positions": float(open_positions),
        }

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

    def get_fno_ltp(self, trading_symbol: str, *, exchange: str = "NSE") -> float | None:
        """Last traded price for an FNO contract (NSE_SYMBOL format)."""
        symbol_key = f"{exchange}_{trading_symbol}"
        payload = self._get(
            "/v1/live-data/ltp",
            {"segment": "FNO", "exchange_symbols": symbol_key},
        )
        if not isinstance(payload, dict):
            return None
        if symbol_key in payload:
            try:
                return float(payload[symbol_key])
            except (TypeError, ValueError):
                pass
        for value in payload.values():
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def get_order_status(self, groww_order_id: str, *, segment: str = "FNO") -> dict[str, Any] | None:
        payload = self._get(f"/v1/order/status/{groww_order_id}", {"segment": segment})
        return payload if isinstance(payload, dict) else None

    def _limit_price(self, ltp: float, transaction_type: str) -> float:
        slip = GROWW_LIMIT_SLIPPAGE_PTS
        side = transaction_type.upper()
        if side == "BUY":
            return round(ltp + slip, 2)
        return round(max(ltp - slip, 0.05), 2)

    def _order_filled(self, status_row: dict[str, Any]) -> bool:
        status = str(status_row.get("order_status") or "").upper()
        if status in _FILL_STATUSES:
            return True
        try:
            return int(status_row.get("filled_quantity") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _order_rejected(self, status_row: dict[str, Any]) -> bool:
        status = str(status_row.get("order_status") or "").upper()
        return status in _REJECT_STATUSES

    def _wait_for_fill(self, groww_order_id: str, *, segment: str = "FNO") -> dict[str, Any] | None:
        last: dict[str, Any] | None = None
        polls = max(1, GROWW_ORDER_STATUS_POLLS)
        for i in range(polls):
            row = self.get_order_status(groww_order_id, segment=segment)
            if not row:
                time.sleep(GROWW_ORDER_STATUS_POLL_SEC)
                continue
            last = row
            if self._order_filled(row):
                return row
            if self._order_rejected(row):
                return row
            status = str(row.get("order_status") or "").upper()
            # Groww often reports APPROVED before EXECUTED; keep polling in-flight orders.
            if status in _IN_FLIGHT_ORDER_STATUSES and i == polls - 1:
                for _ in range(8):
                    time.sleep(GROWW_ORDER_STATUS_POLL_SEC)
                    row = self.get_order_status(groww_order_id, segment=segment)
                    if not row:
                        continue
                    last = row
                    if self._order_filled(row):
                        return row
                    if self._order_rejected(row):
                        return row
            time.sleep(GROWW_ORDER_STATUS_POLL_SEC)
        return last

    def place_market_order(
        self,
        trading_symbol: str,
        quantity: int,
        transaction_type: str,
        *,
        exchange: str = "NSE",
        segment: str = "FNO",
    ) -> str | None:
        """Place MIS FNO order. Default LIMIT@LTP (Groww MARKET can fail LPP RMS)."""
        ref = _order_reference_id(self.username)
        side = transaction_type.upper()
        order_type = GROWW_FNO_ORDER_TYPE
        limit_price: float | None = None
        if order_type == "LIMIT":
            limit_price = self.get_fno_ltp(trading_symbol, exchange=exchange)
            if limit_price is None:
                logger.warning(
                    "[%s] Groww LTP unavailable for %s — falling back to MARKET",
                    self.username,
                    trading_symbol,
                )
                order_type = "MARKET"
            else:
                limit_price = self._limit_price(limit_price, side)

        body: dict[str, Any] = {
            "trading_symbol": trading_symbol,
            "quantity": int(quantity),
            "validity": "DAY",
            "exchange": exchange,
            "segment": segment,
            "product": "MIS",
            "order_type": order_type,
            "transaction_type": side,
            "order_reference_id": ref,
        }
        if order_type == "LIMIT" and limit_price is not None:
            body["price"] = limit_price

        data = self._post("/v1/order/create", body)
        if not data:
            return None
        order_id = str(data.get("groww_order_id") or "")
        if not order_id:
            return None

        status_row = self._wait_for_fill(order_id, segment=segment)
        if status_row is None:
            logger.error("[%s] Groww order %s — status poll failed", self.username, order_id)
            return None
        if not self._order_filled(status_row):
            remark = str(status_row.get("remark") or data.get("remark") or status_row.get("order_status") or "")
            status = str(status_row.get("order_status") or "").upper()
            if status in _CANCELLABLE_ORDER_STATUSES:
                logger.warning(
                    "[%s] Groww order %s still %s — cancelling to avoid duplicate catch-up entry",
                    self.username,
                    order_id,
                    status,
                )
                self.cancel_order(order_id, segment=segment)
            elif status in _IN_FLIGHT_ORDER_STATUSES:
                logger.warning(
                    "[%s] Groww order %s still %s after extended poll — not cancelling in-flight order",
                    self.username,
                    order_id,
                    status,
                )
            logger.error(
                "[%s] Groww order NOT filled: %s %d x %s (%s) — %s",
                self.username,
                side,
                quantity,
                trading_symbol,
                order_id,
                remark[:240],
            )
            return None

        avg = status_row.get("average_fill_price")
        logger.info(
            "[%s] Groww order filled: %s %d x %s @ %s (%s %s)",
            self.username,
            side,
            quantity,
            trading_symbol,
            avg if avg is not None else limit_price,
            order_id,
            order_type,
        )
        return order_id
