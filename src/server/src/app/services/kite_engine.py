"""Kite Connect — index FNO resolve + market orders for S3 fan-out."""

from __future__ import annotations

import csv
import io
import logging
import os
import time
from datetime import date
from typing import Any, Final

import requests

from app.config.paths import ensure_repo_and_lib_on_path, server_root
from app.services.upstox_engine import INDEX_CONFIGS, _format_future_contract_label

ensure_repo_and_lib_on_path()

from kite_credentials_store import (  # noqa: E402
    DEFAULT_KITE_BASE_URL,
    kite_auth_header,
    read_credentials_file_for_user,
)

logger = logging.getLogger("ak07.kite_engine")

PAPER_TRADING: Final[bool] = os.environ.get("AK07_PAPER_TRADING", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

_INDEX_KITE: Final[dict[str, tuple[str, str]]] = {
    "NIFTY": ("NIFTY", "NFO"),
    "BANKNIFTY": ("BANKNIFTY", "NFO"),
    "SENSEX": ("SENSEX", "BFO"),
}

_KITE_INSTRUMENT_URL: Final[dict[str, str]] = {
    "NFO": "https://api.kite.trade/instruments/NFO",
    "BFO": "https://api.kite.trade/instruments/BFO",
}

_INSTR_CACHE_DIR: Final = server_root() / "data" / "instruments"


def _cache_path(exchange: str) -> Any:
    return _INSTR_CACHE_DIR / f"kite_{exchange.lower()}.csv"


def _ensure_kite_instruments(exchange: str) -> bool:
    path = _cache_path(exchange)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < 12:
            return True
    url = _KITE_INSTRUMENT_URL.get(exchange.upper())
    if not url:
        return path.exists()
    try:
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        path.write_bytes(response.content)
        return True
    except requests.RequestException as exc:
        logger.warning("Kite instrument CSV download failed for %s: %s", exchange, exc)
        return path.exists()


def _index_prefix_ok(tradingsymbol: str, underlying: str) -> bool:
    sym = tradingsymbol.upper()
    u = underlying.upper()
    if u == "NIFTY":
        if sym.startswith("BANKNIFTY") or sym.startswith("FINNIFTY") or sym.startswith("MIDCPNIFTY"):
            return False
        if "NXT" in sym:
            return False
    return sym.startswith(u)


def _parse_expiry(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    return text[:10]


def _load_index_fno_rows(
    index_code: str,
    *,
    option_types: frozenset[str] | None = None,
) -> list[dict[str, str]]:
    """Load cached Kite instrument rows for an index (futures or options)."""
    code = index_code.upper()
    underlying, exchange = _INDEX_KITE.get(code, (code, "NFO"))
    if not _ensure_kite_instruments(exchange):
        return []
    path = _cache_path(exchange)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Kite CSV read failed for %s: %s", exchange, exc)
        return []

    today = date.today().isoformat()
    want_opts = option_types is not None
    rows: list[dict[str, str]] = []
    for row in csv.DictReader(io.StringIO(text)):
        if str(row.get("exchange") or "").upper() != exchange:
            continue
        name = str(row.get("name") or "").upper()
        sym = str(row.get("tradingsymbol") or "").strip()
        if not sym or not _index_prefix_ok(sym, underlying):
            continue
        if name and name != underlying and not sym.upper().startswith(underlying):
            continue
        inst = str(row.get("instrument_type") or "").upper()
        if want_opts:
            if inst not in option_types:
                continue
        elif inst in ("CE", "PE"):
            continue
        elif inst != "FUT":
            continue
        expiry = _parse_expiry(str(row.get("expiry") or ""))
        if not expiry or expiry < today:
            continue
        default_lot = INDEX_CONFIGS[code].lot_size if code in INDEX_CONFIGS else 65
        rows.append(
            {
                "tradingsymbol": sym,
                "exchange": exchange,
                "expiry_date": expiry,
                "lot_size": str(row.get("lot_size") or default_lot),
                "instrument_type": inst,
                "strike_price": str(row.get("strike") or "").strip(),
                "instrument_token": str(row.get("instrument_token") or "").strip(),
            }
        )
    rows.sort(key=lambda r: (r["expiry_date"], r.get("strike_price") or "0"))
    return rows


def _kite_instrument_key(exchange: str, tradingsymbol: str) -> str:
    return f"{exchange}:{tradingsymbol}"


class KiteClient:
    def __init__(self, username: str) -> None:
        self.username = username
        self._creds = read_credentials_file_for_user(username)
        self.base_url = (self._creds.get("base_url") or DEFAULT_KITE_BASE_URL).rstrip("/")
        from broker_http import session_for_user

        self.session = session_for_user(username)
        self.session.headers.update({"X-Kite-Version": "3", "Accept": "application/json"})
        self.refresh_auth_from_disk()

    def has_token(self) -> bool:
        return bool((self._creds.get("api_key") or "").strip() and (self._creds.get("access_token") or "").strip())

    def refresh_auth_from_disk(self) -> bool:
        self._creds = read_credentials_file_for_user(self.username)
        self.base_url = (self._creds.get("base_url") or DEFAULT_KITE_BASE_URL).rstrip("/")
        self.session.headers.update(kite_auth_header(self._creds))
        if not self.has_token():
            logger.warning("[%s] No Kite access token on disk", self.username)
            return False
        return True

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.has_token():
            return None
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                params=params,
                timeout=30,
            )
            if response.status_code != 200:
                logger.warning(
                    "[%s] Kite GET %s -> HTTP %d %s",
                    self.username,
                    path,
                    response.status_code,
                    response.text[:200],
                )
                return None
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status") != "success":
                logger.warning("[%s] Kite GET %s failed: %s", self.username, path, str(payload)[:200])
                return None
            data = payload.get("data")
            return data if isinstance(data, dict) else {"_list": data}
        except (requests.RequestException, ValueError) as exc:
            logger.warning("[%s] Kite GET %s error: %s", self.username, path, exc)
            return None

    def _post_form(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if not self.has_token():
            logger.warning("[%s] Kite POST skipped — no access token", self.username)
            return None
        try:
            response = self.session.post(
                f"{self.base_url}{path}",
                data=body,
                timeout=30,
            )
            payload: dict[str, Any] = {}
            try:
                payload = response.json() if response.text else {}
            except ValueError:
                payload = {}
            if response.status_code != 200 or payload.get("status") != "success":
                err = payload.get("message") or payload.get("error_type") or response.text[:300]
                logger.warning(
                    "[%s] Kite POST %s failed HTTP %d — %s",
                    self.username,
                    path,
                    response.status_code,
                    err,
                )
                return None
            data = payload.get("data")
            return data if isinstance(data, dict) else {"order_id": data}
        except requests.RequestException as exc:
            logger.warning("[%s] Kite POST %s error: %s", self.username, path, exc)
            return None

    def get_fno_positions(self) -> list[dict[str, Any]]:
        data = self._get("/portfolio/positions")
        if not data:
            return []
        rows: list[dict[str, Any]] = []
        for bucket in ("net", "day"):
            part = data.get(bucket)
            if not isinstance(part, list):
                continue
            for row in part:
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    def net_fno_quantity(self, exchange: str, tradingsymbol: str) -> int:
        sym = tradingsymbol.upper()
        ex = exchange.upper()
        for row in self.get_fno_positions():
            if str(row.get("tradingsymbol") or "").upper() != sym:
                continue
            if str(row.get("exchange") or "").upper() != ex:
                continue
            try:
                return int(row.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
        return 0

    def has_directional_exposure(
        self,
        exchange: str,
        tradingsymbol: str,
        direction: str,
        min_qty: int = 1,
    ) -> bool:
        qty = self.net_fno_quantity(exchange, tradingsymbol)
        if direction == "LONG":
            return qty >= min_qty
        return qty <= -min_qty

    def get_fno_ltp(self, exchange: str, tradingsymbol: str) -> float | None:
        key = _kite_instrument_key(exchange, tradingsymbol)
        data = self._get("/quote/ltp", params={"i": key})
        if not data:
            return None
        row = data.get(key)
        if not isinstance(row, dict):
            return None
        try:
            return float(row.get("last_price"))
        except (TypeError, ValueError):
            return None

    def get_index_future_contract(self, index_code: str) -> dict[str, Any] | None:
        code = index_code.upper()
        rows = _load_index_fno_rows(code, option_types=None)
        if not rows:
            logger.warning("[%s] No Kite %s futures in instrument cache", self.username, code)
            return None
        row = rows[0]
        exchange = row["exchange"]
        sym = row["tradingsymbol"]
        expiry = row["expiry_date"]
        lot = int(row.get("lot_size") or INDEX_CONFIGS.get(code, None) and INDEX_CONFIGS[code].lot_size or 65)
        return {
            "tradingsymbol": sym,
            "exchange": exchange,
            "expiry": expiry,
            "contract_label": _format_future_contract_label(sym, expiry),
            "lot_size": lot,
            "instrument_key": f"kite:{_kite_instrument_key(exchange, sym)}",
        }

    def get_itm_option_contract(
        self,
        index_code: str,
        spot: float,
        direction: str,
        *,
        itm_offset: float = 50.0,
        force_strike: int | None = None,
    ) -> dict[str, Any] | None:
        """Mild-ITM CE (LONG) or PE (SHORT). ``force_strike`` locks S3 to the shared Upstox pick."""
        from app.services.options_greeks import preferred_itm_strikes

        code = index_code.upper()
        opt = "CE" if direction == "LONG" else "PE"
        cfg = INDEX_CONFIGS.get(code)
        step = cfg.strike_step if cfg else 50
        atm = int(round(spot / step) * step)
        preferred = preferred_itm_strikes(spot, step, direction)

        rows = _load_index_fno_rows(code, option_types=frozenset({opt}))
        by_expiry: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            by_expiry.setdefault(row["expiry_date"], []).append(row)
        if not by_expiry:
            logger.warning("[%s] No Kite %s %s options found", self.username, code, opt)
            return None
        nearest_exp = sorted(by_expiry.keys())[0]
        pool = by_expiry[nearest_exp]
        by_strike: dict[int, dict[str, str]] = {}
        for row in pool:
            try:
                strike = int(float(row.get("strike_price") or 0))
            except (TypeError, ValueError):
                continue
            if strike > 0:
                by_strike[strike] = row

        best: dict[str, str] | None = None
        selection = "itm_first_spot_aligned"
        if force_strike and int(force_strike) in by_strike:
            best = by_strike[int(force_strike)]
            selection = "shared_s3_strike"
        else:
            if force_strike:
                logger.warning(
                    "[%s] Kite missing forced strike %s%s — falling back to ITM-first",
                    self.username,
                    force_strike,
                    opt,
                )
            for strike in preferred:
                if strike in by_strike:
                    best = by_strike[strike]
                    break
            if not best:
                if not by_strike:
                    return None
                strike_i = min(by_strike.keys(), key=lambda s: abs(s - atm))
                best = by_strike[strike_i]

        strike_i = int(float(best.get("strike_price") or 0))
        exchange = best["exchange"]
        sym = best["tradingsymbol"]
        lot = int(best.get("lot_size") or (cfg.lot_size if cfg else 65))
        delta = None
        try:
            from app.services.options_greeks import bs_delta, years_to_expiry

            delta = bs_delta(spot, strike_i, years_to_expiry(nearest_exp), 0.18, option_type=opt)
        except Exception:
            pass
        logger.info(
            "[%s] Kite option pick %s %s%d %s ≈ATM %d (spot=%.2f delta≈%s)",
            self.username,
            direction,
            opt,
            strike_i,
            selection,
            atm,
            spot,
            f"{abs(delta):.2f}" if delta is not None else "?",
        )
        return {
            "tradingsymbol": sym,
            "exchange": exchange,
            "expiry": nearest_exp,
            "contract_label": f"{code} {strike_i}{opt}",
            "lot_size": lot,
            "instrument_key": f"kite:{_kite_instrument_key(exchange, sym)}",
            "strike": strike_i,
            "option_type": opt,
            "delta": delta,
            "selection": selection,
        }

    def place_market_order(
        self,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        transaction_type: str,
        *,
        bypass_profit_guard: bool = False,
    ) -> str | None:
        side = transaction_type.upper()
        if (
            not bypass_profit_guard
            and side == "BUY"
            and not PAPER_TRADING
        ):
            from app.services.daily_profit_guard import profit_target_engaged  # noqa: PLC0415

            if profit_target_engaged():
                logger.warning(
                    "[%s] Kite BUY blocked — daily profit target hit (%s:%s x %d)",
                    self.username,
                    exchange,
                    tradingsymbol,
                    quantity,
                )
                return None

        body = {
            "variety": "regular",
            "exchange": exchange.upper(),
            "tradingsymbol": tradingsymbol,
            "transaction_type": side,
            "quantity": int(quantity),
            "product": "MIS",
            "order_type": "MARKET",
            "validity": "DAY",
            "tag": "ak07s3",
            # Kite rejects bare API MARKET orders. -1 asks Zerodha to apply
            # automatic market protection under its current algo guidelines.
            "market_protection": -1,
        }
        data = self._post_form("/orders/regular", body)
        if not data:
            return None
        order_id = str(data.get("order_id") or "")
        if not order_id:
            return None
        if side == "BUY" and not bypass_profit_guard:
            from app.services.daily_profit_guard import record_broker_entry  # noqa: PLC0415

            record_broker_entry()
        logger.info(
            "[%s] Kite order placed: %s %d x %s:%s (%s)",
            self.username,
            side,
            quantity,
            exchange,
            tradingsymbol,
            order_id,
        )
        return order_id


def build_kite_client(username: str) -> KiteClient:
    return KiteClient(username)
