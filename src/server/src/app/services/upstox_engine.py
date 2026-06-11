"""AK07 multi-index execution engine (Nifty 50 / BankNifty / Sensex).

Responsibilities:
- Track each index's heavyweight components via live Upstox quotes and derive
  a majority-vote Component Bias (BULLISH / BEARISH / NEUTRAL).
- Locate the institutional Call Wall (highest call OI strike) and Put Floor
  (highest put OI strike) from the daily Upstox option chain.
- On every closed 5-minute candle, evaluate the two structural setups
  (support-pocket long / resistance-pocket short) with wick-rejection and
  component-confluence filters, gated by the AI system bias from Redis.
- On entry, resolve the closest ITM weekly option contract (LONG -> CE near
  spot-50, SHORT -> PE near spot+50) and trade it in 2 lots: book 1 lot at
  half target (+60 pts) or on backend warning signals, run the rest to the
  full +120 pt target or the hard -60 pt stop from entry.
- Daily session automation: at AK07_TOKEN_REFRESH_IST (default 08:45 IST)
  request/refresh the Upstox V3 access token and warm the Redis connection pool; at
  15:30 IST archive the full session (trades, spot tracking, sentiment, P&L,
  Redis cache dump) to app/archive/performance_review_<YYYY-MM-DD>.json.
- Manage risk: max 2 trades per index per day, hard 14:55 IST square-off, and
  an externally-driven kill switch.
- Publish per-index state, open positions, and a heartbeat to Redis so the
  Streamlit cockpit and the MCP context bridge observe without coupling.

Run standalone:  python -m app.services.upstox_engine   (from src/server/src)

Safety: the engine starts in PAPER mode (no real orders) unless
AK07_PAPER_TRADING=0 is set in the environment.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import requests

# Allow running as a plain script as well as a package module.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config.paths import archive_dir
from app.services import cache_manager, telegram_notifier
from app.services import performance_store

logger = logging.getLogger("ak07.upstox_engine")

IST: Final = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Strategy constants
# ---------------------------------------------------------------------------
TARGET_POINTS: Final[float] = 120.0
STOP_LOSS_POINTS: Final[float] = 60.0
PARTIAL_BOOK_POINTS: Final[float] = TARGET_POINTS / 2  # +60: book 1 lot
INITIAL_LOTS: Final[int] = 2
ITM_OFFSET_POINTS: Final[float] = 50.0  # LONG -> CE near spot-50, SHORT -> PE near spot+50
MAX_TRADES_PER_INDEX_PER_DAY: Final[int] = 2
SUPPORT_POCKET_POINTS: Final[float] = 30.0
RESISTANCE_POCKET_POINTS: Final[float] = 30.0
WICK_REJECTION_RATIO: Final[float] = 0.40
SQUARE_OFF_TIME: Final[dtime] = dtime(14, 55)   # IST hard intraday protection
ARCHIVE_TIME: Final[dtime] = dtime(15, 30)      # post-market performance archival


def _parse_ist_time(env_key: str, default_hour: int, default_minute: int) -> dtime:
    """Parse HH:MM from env (e.g. AK07_TOKEN_REFRESH_IST=08:45)."""
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        parts = raw.replace(".", ":").split(":")
        if len(parts) >= 2:
            try:
                return dtime(int(parts[0]), int(parts[1]))
            except ValueError:
                logger.warning(
                    "Invalid %s=%r; using default %02d:%02d IST",
                    env_key,
                    raw,
                    default_hour,
                    default_minute,
                )
    return dtime(default_hour, default_minute)


def _format_ist_time(value: dtime) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


ENTRY_OPEN_TIME: Final[dtime] = _parse_ist_time("AK07_ENTRY_OPEN_IST", 9, 20)
TOKEN_REFRESH_TIME: Final[dtime] = _parse_ist_time("AK07_TOKEN_REFRESH_IST", 8, 45)
WALL_REFRESH_SECONDS: Final[int] = 300
POLL_SECONDS: Final[float] = float(os.environ.get("AK07_POLL_SECONDS", "15"))
PAPER_TRADING: Final[bool] = os.environ.get("AK07_PAPER_TRADING", "0") != "0"
MOCK_MODE: Final[bool] = os.environ.get("AK07_MOCK") == "1"
OI_BAND_POINTS: Final[float] = float(os.environ.get("AK07_OI_BAND_POINTS", "500"))
COMPONENT_BIAS_MIN_KNOWN: Final[int] = int(os.environ.get("AK07_COMPONENT_BIAS_MIN_KNOWN", "2"))

CANDLE_MINUTES: Final[int] = 5

# Legacy historical-candle metadata keys — never merge with live V3 intraday rows.
_HISTORICAL_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"interval", "continuous", "to_date", "from_date", "intraday", "candle_type"}
)

# Persistent session archives (Docker volume: src/server/data/archive)
ARCHIVE_DIR: Final[Path] = archive_dir()
TRADE_LOG_KEY_TEMPLATE: Final[str] = "ak07:trade_log:{day}"

# ---------------------------------------------------------------------------
# Component & asset definitions
# ---------------------------------------------------------------------------
HEAVYWEIGHT_INSTRUMENT_KEYS: Final[dict[str, str]] = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "INFY": "NSE_EQ|INE009A01021",
    "TCS": "NSE_EQ|INE467B01029",
    "SBIN": "NSE_EQ|INE062A01020",
    "AXISBANK": "NSE_EQ|INE238A01034",
    "KOTAKBANK": "NSE_EQ|INE237A01028",
    "ITC": "NSE_EQ|INE154A01025",
}


@dataclass(frozen=True)
class IndexConfig:
    code: str                 # short id used in Redis keys
    display: str              # human-readable name
    spot_instrument_key: str  # index spot key (also used for option chain)
    heavyweights: tuple[str, ...]
    lot_size: int             # units per lot, overridable via env AK07_LOT_SIZE_<CODE>
    strike_step: int          # strike spacing, used for paper-mode strike synthesis


INDEX_CONFIGS: Final[dict[str, IndexConfig]] = {
    "NIFTY": IndexConfig(
        code="NIFTY",
        display="Nifty 50",
        spot_instrument_key="NSE_INDEX|Nifty 50",
        heavyweights=("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"),
        lot_size=int(os.environ.get("AK07_LOT_SIZE_NIFTY", "75")),
        strike_step=50,
    ),
    "BANKNIFTY": IndexConfig(
        code="BANKNIFTY",
        display="BankNifty",
        spot_instrument_key="NSE_INDEX|Nifty Bank",
        heavyweights=("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"),
        lot_size=int(os.environ.get("AK07_LOT_SIZE_BANKNIFTY", "35")),
        strike_step=100,
    ),
    "SENSEX": IndexConfig(
        code="SENSEX",
        display="Sensex",
        spot_instrument_key="BSE_INDEX|SENSEX",
        heavyweights=("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC"),
        lot_size=int(os.environ.get("AK07_LOT_SIZE_SENSEX", "20")),
        strike_step=100,
    ),
}


# ---------------------------------------------------------------------------
# V3 intraday parsing (no legacy historical key overlap)
# ---------------------------------------------------------------------------
def parse_v3_intraday_candles(
    data: Any,
    now: datetime,
) -> list[dict[str, float]] | None:
    """Parse V3 intraday candle payload. Returns None if legacy/historical keys detected."""
    if not isinstance(data, dict):
        return None
    if _HISTORICAL_PAYLOAD_KEYS.intersection(data.keys()):
        logger.warning("Rejected candle payload with historical metadata keys: %s", sorted(data.keys())[:6])
        return None

    raw = data.get("candles")
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning("Rejected non-list V3 candles container (%s)", type(raw).__name__)
        return None

    candles: list[dict[str, float]] = []
    for row in raw:
        candle = _parse_v3_candle_row(row)
        if candle is None:
            continue
        ts = datetime.fromisoformat(candle["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        if ts + timedelta(minutes=CANDLE_MINUTES) <= now.astimezone(ts.tzinfo):
            candles.append(candle)
    candles.sort(key=lambda c: c["timestamp"])
    return candles


def _parse_v3_candle_row(row: Any) -> dict[str, float] | None:
    """Accept modern V3 array rows or object rows; reject legacy parameter dicts."""
    if isinstance(row, dict):
        if _HISTORICAL_PAYLOAD_KEYS.intersection(row.keys()):
            return None
        ts_raw = row.get("timestamp") or row.get("time") or row.get("start_time")
        try:
            return {
                "timestamp": datetime.fromisoformat(str(ts_raw)).isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume") or 0),
            }
        except (KeyError, TypeError, ValueError):
            return None

    if isinstance(row, (list, tuple)) and len(row) >= 6:
        try:
            return {
                "timestamp": datetime.fromisoformat(str(row[0])).isoformat(),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": int(row[5] or 0),
            }
        except (ValueError, TypeError, IndexError):
            return None
    return None


def in_execution_boundary(spot: float, call_wall: int, put_floor: int) -> bool:
    """True when spot is inside the 30-pt support or resistance pocket."""
    in_support = put_floor <= spot <= put_floor + SUPPORT_POCKET_POINTS
    in_resistance = call_wall - RESISTANCE_POCKET_POINTS <= spot <= call_wall
    return in_support or in_resistance


# ---------------------------------------------------------------------------
# Upstox REST client (token sourced from the per-user credentials store)
# ---------------------------------------------------------------------------
class UpstoxClient:
    def __init__(self, username: str = "AK07") -> None:
        from app.config.paths import ensure_repo_and_lib_on_path

        ensure_repo_and_lib_on_path()
        self.username = username
        self.base_url: str = "https://api.upstox.com/v2"
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.refresh_access_token_from_disk()

    def _credentials(self) -> dict[str, str]:
        from upstox_credentials_store import load_upstox_credentials_for_user  # noqa: PLC0415

        return load_upstox_credentials_for_user(self.username)

    def refresh_access_token_from_disk(self) -> bool:
        """Reload the bearer token from the credentials store. True if present."""
        creds = self._credentials()
        self.base_url = creds.get("base_url") or "https://api.upstox.com/v2"
        token = creds.get("access_token") or ""
        self.session.headers["Authorization"] = f"Bearer {token}"
        if not token:
            logger.warning("No Upstox access token on disk for %s; API calls will fail", self.username)
        return bool(token)

    def request_daily_access_token(self) -> bool:
        """Request a fresh V3 access token using the stored App Client ID/Secret."""
        try:
            from upstox_credentials_store import persist_credentials_for_user  # noqa: PLC0415

            creds = self._credentials()
            client_id = (creds.get("api_key") or "").strip()
            client_secret = (creds.get("api_secret") or "").strip()
            if not client_id or not client_secret:
                logger.warning("Token refresh skipped: api_key/api_secret missing from credentials store")
                return self.refresh_access_token_from_disk()

            v3_base = self.base_url.replace("/v2", "/v3")
            response = requests.post(
                f"{v3_base}/login/auth/token/request/{client_id}",
                json={"client_secret": client_secret},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=20,
            )
            body: dict[str, Any] = {}
            try:
                body = response.json() if response.text else {}
            except ValueError:
                pass

            token = str(((body.get("data") or {}).get("access_token")) or body.get("access_token") or "")
            if response.status_code == 200 and token:
                persist_credentials_for_user(
                    self.username,
                    {**creds, "access_token": token},
                )
                logger.info("Upstox V3 access token refreshed and persisted (HTTP 200)")
                return self.refresh_access_token_from_disk()
            elif response.status_code == 200:
                logger.info(
                    "Upstox V3 token request accepted; token will arrive via notifier webhook"
                )
                return self.refresh_access_token_from_disk()
            logger.error(
                "Upstox V3 token request failed: HTTP %d %s",
                response.status_code,
                str(response.text or "")[:300],
            )
            return False
        except Exception as exc:  # Bound exception explicitly for debugging safely
            logger.exception("Unexpected failure during daily token refresh: %s", exc)
            return False

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            response = self.session.get(url, params=params, timeout=15)
            if response.status_code != 200:
                logger.warning("Upstox GET %s -> HTTP %d %s", url, response.status_code, response.text[:200])
                return None
            payload = response.json()
            if payload.get("status") != "success":
                logger.warning("Upstox GET %s -> status=%s", url, payload.get("status"))
                return None
            return payload.get("data")
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Upstox GET %s failed: %s", url, exc)
            return None

    def get_ltp(self, instrument_key: str) -> float | None:
        data = self._get(f"{self.base_url}/market-quote/ltp", {"instrument_key": instrument_key})
        if not isinstance(data, dict):
            return None
        for row in data.values():
            ltp = row.get("last_price")
            if ltp is not None:
                return float(ltp)
        return None

    def get_ohlc_quotes(self, instrument_keys: list[str]) -> dict[str, dict[str, float]]:
        """Daily OHLC + LTP for many instruments. Returns {instrument_key: {ltp, open}}."""
        data = self._get(
            f"{self.base_url}/market-quote/ohlc",
            {"instrument_key": ",".join(instrument_keys), "interval": "1d"},
        )
        out: dict[str, dict[str, float]] = {}
        if not isinstance(data, dict):
            return out
        for row in data.values():
            key = row.get("instrument_token") or ""
            ltp = row.get("last_price")
            day_open = (row.get("ohlc") or {}).get("open")
            if key and ltp is not None and day_open:
                out[key] = {"ltp": float(ltp), "open": float(day_open)}
        return out

    def get_closed_5min_candles(self, instrument_key: str) -> list[dict[str, float]] | None:
        """Today's completed 5-min candles from V3 intraday. None = legacy payload rejected."""
        v3_base = self.base_url.replace("/v2", "/v3")
        data = self._get(
            f"{v3_base}/historical-candle/intraday/{instrument_key}/minutes/{CANDLE_MINUTES}"
        )
        return parse_v3_intraday_candles(data, datetime.now(IST))

    def _nearest_expiry(self, instrument_key: str) -> str | None:
        data = self._get(f"{self.base_url}/option/contract", {"instrument_key": instrument_key})
        if not isinstance(data, list):
            return None
        today = date.today().isoformat()
        expiries = sorted({str(c.get("expiry")) for c in data if str(c.get("expiry", "")) >= today})
        return expiries[0] if expiries else None

    def _fetch_nearest_expiry_chain(self, instrument_key: str) -> list[dict[str, Any]]:
        """Rows of the active weekly (nearest-expiry) option chain."""
        expiry = self._nearest_expiry(instrument_key)
        if not expiry:
            return []
        data = self._get(
            f"{self.base_url}/option/chain",
            {"instrument_key": instrument_key, "expiry_date": expiry},
        )
        return data if isinstance(data, list) else []

    def get_itm_option_contract(
        self, instrument_key: str, spot: float, direction: str
    ) -> dict[str, Any] | None:
        """Exact instrument for the closest ITM contract from the weekly chain."""
        rows = self._fetch_nearest_expiry_chain(instrument_key)
        if not rows:
            return None
        leg = "call_options" if direction == "LONG" else "put_options"
        desired = spot - ITM_OFFSET_POINTS if direction == "LONG" else spot + ITM_OFFSET_POINTS

        best: dict[str, Any] | None = None
        best_distance = float("inf")
        for row in rows:
            try:
                strike = float(row.get("strike_price", 0))
            except (TypeError, ValueError):
                continue
            contract_key = str(((row.get(leg) or {}).get("instrument_key")) or "")
            if not contract_key:
                continue
            distance = abs(strike - desired)
            if distance < best_distance:
                best_distance = distance
                best = {
                    "instrument_key": contract_key,
                    "strike": int(strike),
                    "option_type": "CE" if direction == "LONG" else "PE",
                }
        return best

    def get_oi_walls(self, instrument_key: str, spot: float | None = None) -> tuple[int, int] | None:
        """(call_wall_strike, put_floor_strike) from the nearest-expiry chain."""
        data = self._fetch_nearest_expiry_chain(instrument_key)
        if not data:
            return None

        def _scan(restrict_to_band: bool) -> tuple[int, int] | None:
            best_call: tuple[float, int] | None = None
            best_put: tuple[float, int] | None = None
            for row in data:
                try:
                    strike = int(float(row.get("strike_price", 0)))
                except (TypeError, ValueError):
                    continue
                if restrict_to_band and spot is not None and abs(strike - spot) > OI_BAND_POINTS:
                    continue
                call_oi = float(((row.get("call_options") or {}).get("market_data") or {}).get("oi") or 0)
                put_oi = float(((row.get("put_options") or {}).get("market_data") or {}).get("oi") or 0)
                if best_call is None or call_oi > best_call[0]:
                    best_call = (call_oi, strike)
                if best_put is None or put_oi > best_put[0]:
                    best_put = (put_oi, strike)
            if not best_call or not best_put or best_call[0] <= 0 or best_put[0] <= 0:
                return None
            return best_call[1], best_put[1]

        if spot is not None:
            banded = _scan(restrict_to_band=True)
            if banded:
                return banded
            logger.info(
                "OI walls: nothing within %.0f pts of spot %.2f for %s; using full chain",
                OI_BAND_POINTS,
                spot,
                instrument_key,
            )
        return _scan(restrict_to_band=False)

    def place_market_order(self, instrument_key: str, quantity: int, transaction_type: str) -> bool:
        """Market order via the standard then HFT endpoint."""
        payload = {
            "quantity": quantity,
            "product": "I",
            "validity": "DAY",
            "price": 0,
            "tag": "ak07_engine",
            "instrument_token": instrument_key,
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        for url in (f"{self.base_url}/order/place", "https://api-hft.upstox.com/v2/order/place"):
            try:
                response = self.session.post(url, json=payload, timeout=15)
                body = response.json() if response.text else {}
                if response.status_code == 200 and body.get("status") == "success":
                    logger.info("Order placed: %s %d x %s", transaction_type, quantity, instrument_key)
                    return True
                logger.warning(
                    "Order rejected at %s: HTTP %d %s", url, response.status_code, str(body)[:250]
                )
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Order error at %s: %s", url, exc)
        logger.error("Order FAILED on all endpoints: %s %d x %s", transaction_type, quantity, instrument_key)
        return False


class MockUpstoxClient(UpstoxClient):
    """Simulated V3 feed for AK07_MOCK cockpit/engine verification (no broker I/O)."""

    def __init__(self) -> None:
        self.username = "AK07"
        self.base_url = "https://api.upstox.com/v2"
        self.session = requests.Session()
        self._tick = 0
        self._walls: dict[str, tuple[int, int]] = {
            "NIFTY": (24_000, 21_200),
            "BANKNIFTY": (52_500, 50_800),
            "SENSEX": (79_800, 77_900),
        }

    def refresh_access_token_from_disk(self) -> bool:
        return True

    def request_daily_access_token(self) -> bool:
        return True

    def get_ltp(self, instrument_key: str) -> float | None:
        code = _index_code_for_spot_key(instrument_key)
        if code is None:
            return None
        call_wall, put_floor = self._walls[code]
        if self._tick >= 3:
            return float(put_floor + 10)
        return float((call_wall + put_floor) / 2)

    def get_ohlc_quotes(self, instrument_keys: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for key in instrument_keys:
            ltp = 100.0 + (self._tick % 5)
            out[key] = {"ltp": ltp, "open": 99.0}
        return out

    def get_closed_5min_candles(self, instrument_key: str) -> list[dict[str, float]] | None:
        code = _index_code_for_spot_key(instrument_key)
        if code is None:
            return []
        call_wall, put_floor = self._walls[code]
        now = datetime.now(IST)
        if self._tick < 3:
            close = float((call_wall + put_floor) / 2)
            low = close - 5
            high = close + 5
        else:
            close = float(put_floor + 10)
            low = put_floor + 2
            high = close + 8
        return [
            {
                "timestamp": (now - timedelta(minutes=CANDLE_MINUTES)).isoformat(),
                "open": close - 2,
                "high": high,
                "low": low,
                "close": close,
                "volume": 120_000,
            }
        ]

    def get_oi_walls(self, instrument_key: str, spot: float | None = None) -> tuple[int, int] | None:
        code = _index_code_for_spot_key(instrument_key)
        if code is None:
            return None
        return self._walls[code]

    def get_itm_option_contract(
        self, instrument_key: str, spot: float, direction: str
    ) -> dict[str, Any] | None:
        code = _index_code_for_spot_key(instrument_key)
        step = INDEX_CONFIGS[code].strike_step if code else 50
        desired = spot - ITM_OFFSET_POINTS if direction == "LONG" else spot + ITM_OFFSET_POINTS
        strike = int(round(desired / step) * step)
        return {
            "instrument_key": "",
            "strike": strike,
            "option_type": "CE" if direction == "LONG" else "PE",
        }

    def place_market_order(self, instrument_key: str, quantity: int, transaction_type: str) -> bool:
        logger.info("MOCK order: %s %d x %s", transaction_type, quantity, instrument_key or "paper")
        return True

    def advance_tick(self) -> None:
        self._tick += 1


def _index_code_for_spot_key(instrument_key: str) -> str | None:
    for code, cfg in INDEX_CONFIGS.items():
        if cfg.spot_instrument_key == instrument_key:
            return code
    return None


def build_upstox_client() -> UpstoxClient:
    if MOCK_MODE:
        logger.info("AK07_MOCK=1 -> using MockUpstoxClient (simulated V3 feed)")
        return MockUpstoxClient()
    return UpstoxClient()


# ---------------------------------------------------------------------------
# Pure strategy functions (unit-testable, no I/O)
# ---------------------------------------------------------------------------
def component_changes(
    heavyweights: tuple[str, ...], quotes: dict[str, dict[str, float]]
) -> dict[str, float | None]:
    """Intraday %change vs open for each heavyweight: (LTP - Open) / Open * 100."""
    out: dict[str, float | None] = {}
    for symbol in heavyweights:
        row = quotes.get(HEAVYWEIGHT_INSTRUMENT_KEYS[symbol])
        if row and row["open"]:
            out[symbol] = round((row["ltp"] - row["open"]) / row["open"] * 100, 2)
        else:
            out[symbol] = None
    return out


def component_bias(changes: dict[str, float | None]) -> str:
    """Majority vote: BULLISH if most quoted heavyweights are green, BEARISH if red."""
    known = [c for c in changes.values() if c is not None]
    if len(known) < COMPONENT_BIAS_MIN_KNOWN:
        return "NEUTRAL"
    positives = sum(1 for c in known if c > 0)
    negatives = sum(1 for c in known if c < 0)
    majority = len(known) / 2
    if positives > majority:
        return "BULLISH"
    if negatives > majority:
        return "BEARISH"
    return "NEUTRAL"


def lower_wick_ratio(candle: dict[str, float]) -> float:
    rng = candle["high"] - candle["low"]
    return (candle["close"] - candle["low"]) / rng if rng > 0 else 0.0


def upper_wick_ratio(candle: dict[str, float]) -> float:
    rng = candle["high"] - candle["low"]
    return (candle["high"] - candle["close"]) / rng if rng > 0 else 0.0


def detect_setup(
    candle: dict[str, float],
    call_wall: int,
    put_floor: int,
    comp_bias: str,
    system_bias: str,
) -> str | None:
    """Evaluate SETUP 1 (LONG) and SETUP 2 (SHORT) on a closed 5-min candle."""
    spot = candle["close"]

    in_support_pocket = put_floor <= spot <= put_floor + SUPPORT_POCKET_POINTS
    if (
        in_support_pocket
        and lower_wick_ratio(candle) >= WICK_REJECTION_RATIO
        and comp_bias in ("BULLISH", "NEUTRAL")
        and system_bias != "SHORT_ONLY"
    ):
        return "LONG"

    in_resistance_pocket = call_wall - RESISTANCE_POCKET_POINTS <= spot <= call_wall
    if (
        in_resistance_pocket
        and upper_wick_ratio(candle) >= WICK_REJECTION_RATIO
        and comp_bias in ("BEARISH", "NEUTRAL")
        and system_bias != "LONG_ONLY"
    ):
        return "SHORT"

    return None


# ---------------------------------------------------------------------------
# Per-index runtime state
# ---------------------------------------------------------------------------
@dataclass
class Position:
    index_code: str
    direction: str          # LONG | SHORT
    entry_price: float      # index spot at entry
    target_price: float
    sl_price: float
    lot_size: int
    lots_remaining: int     # starts at INITIAL_LOTS, drops to 1 after partial book
    partial_booked: bool
    instrument_key: str     # ITM option contract
    option_strike: int
    option_type: str        # CE | PE
    opened_at: str

    @property
    def quantity(self) -> int:
        return self.lot_size * self.lots_remaining

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "quantity": self.quantity}


@dataclass
class IndexState:
    config: IndexConfig
    spot: float | None = None
    call_wall: int | None = None
    put_floor: int | None = None
    walls_refreshed_at: float = 0.0
    changes: dict[str, float | None] = field(default_factory=dict)
    comp_bias: str = "NEUTRAL"
    trades_today: int = 0
    trade_day: str = ""
    position: Position | None = None
    last_candle_ts: str = ""
    day_volume: int = 0


def reset_index_live_cache(state: IndexState) -> None:
    """Drop stale in-memory fields for one index."""
    state.spot = None
    state.call_wall = None
    state.put_floor = None
    state.walls_refreshed_at = 0.0
    state.changes = {}
    state.comp_bias = "NEUTRAL"
    state.last_candle_ts = ""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class AK07Engine:
    def __init__(self) -> None:
        self.client = build_upstox_client()
        self.states: dict[str, IndexState] = {
            code: IndexState(config=cfg) for code, cfg in INDEX_CONFIGS.items()
        }
        self.session_entries_blocked = False
        self.trade_log: list[dict[str, Any]] = []
        self.spot_track: dict[str, list[dict[str, Any]]] = {c: [] for c in INDEX_CONFIGS}
        self.sentiment_track: dict[str, list[dict[str, Any]]] = {c: [] for c in INDEX_CONFIGS}
        self.realized_pnl_points: dict[str, float] = {c: 0.0 for c in INDEX_CONFIGS}
        self.token_refresh_day = ""
        self.archived_day = ""
        logger.info("AK07 engine initialized (paper_trading=%s)", PAPER_TRADING)

    def run(self) -> None:
        logger.info("AK07 engine loop starting (poll every %.0fs)", POLL_SECONDS)
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:  # Bound for diagnostic safety
                logger.exception("Engine tick failed; continuing: %s", exc)
            time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))

    def tick(self) -> None:
        now = datetime.now(IST)
        if not isinstance(self.client, MockUpstoxClient):
            self.client.refresh_access_token_from_disk()
        self._roll_trade_day(now)
        self._daily_session_init(now)
        self._refresh_session_entry_gate(now)

        system_bias = cache_manager.get_system_bias()
        for state in self.states.values():
            self._process_index(state, system_bias, now)

        self._publish_global_state(now)
        self._maybe_archive_day(now)

        if isinstance(self.client, MockUpstoxClient):
            self.client.advance_tick()

    def _refresh_session_entry_gate(self, now: datetime) -> None:
        """Session-level entry halt: kill switch, 14:55 square-off, pre-9:20 open skip."""
        if MOCK_MODE:
            if self._kill_switch_engaged():
                self.square_off_all("KILL_SWITCH")
                self.session_entries_blocked = True
                logger.info("entries blocked (kill switch engaged)")
            else:
                self.session_entries_blocked = False
            return

        if self._kill_switch_engaged():
            self.square_off_all("KILL_SWITCH")
            self.session_entries_blocked = True
            logger.info("entries blocked (kill switch engaged)")
        elif now.time() >= SQUARE_OFF_TIME:
            if any(s.position for s in self.states.values()):
                telegram_notifier.notify_system_event(
                    "TIME GATE 14:55 IST", "Entry logic halted; squaring off all active positions."
                )
            self.square_off_all("TIME_GATE_1455")
            self.session_entries_blocked = True
            logger.info("entries blocked (14:55 IST time gate)")
        elif now.time() < ENTRY_OPEN_TIME:
            self.session_entries_blocked = True
            logger.debug("entries blocked (pre-9:20 opening rotation skip)")
        else:
            self.session_entries_blocked = False

    def _daily_session_init(self, now: datetime) -> None:
        """09:15 IST automation: V3 token refresh + Redis pool warm-up."""
        today = now.date().isoformat()
        if self.token_refresh_day == today or now.time() < TOKEN_REFRESH_TIME:
            return
        self.token_refresh_day = today
        refresh_at = _format_ist_time(TOKEN_REFRESH_TIME)
        logger.info("Daily session init (%s IST): requesting Upstox V3 access token", refresh_at)
        token_ok = self.client.request_daily_access_token()
        redis_ok = cache_manager.set_json(
            cache_manager.ENGINE_HEARTBEAT_KEY,
            {"at": now.isoformat(), "paper_trading": PAPER_TRADING, "session_init": True},
            ttl_seconds=60,
        )
        telegram_notifier.notify_system_event(
            "DAILY SESSION INIT",
            f"{refresh_at} IST startup - token {'OK' if token_ok else 'PENDING/FAILED'}, "
            f"Redis pool {'warm' if redis_ok else 'UNREACHABLE'}.",
        )

    def _process_index(self, state: IndexState, system_bias: str, now: datetime) -> None:
        cfg = state.config

        try:
            spot = self.client.get_ltp(cfg.spot_instrument_key)
            if spot is not None:
                state.spot = spot

            keys = [HEAVYWEIGHT_INSTRUMENT_KEYS[s] for s in cfg.heavyweights]
            quotes = self.client.get_ohlc_quotes(keys)
            if quotes:
                state.changes = component_changes(cfg.heavyweights, quotes)
                state.comp_bias = component_bias(state.changes)

            if time.monotonic() - state.walls_refreshed_at > WALL_REFRESH_SECONDS:
                walls = self.client.get_oi_walls(cfg.spot_instrument_key, state.spot)
                if walls:
                    state.call_wall, state.put_floor = walls
                state.walls_refreshed_at = time.monotonic()
        except Exception as exc:
            logger.exception("[%s] live V3 feed read failed; resetting index cache: %s", cfg.code, exc)
            reset_index_live_cache(state)
            walls = self.client.get_oi_walls(cfg.spot_instrument_key, state.spot)
            if walls:
                state.call_wall, state.put_floor = walls
                state.walls_refreshed_at = time.monotonic()

        if state.spot is not None:
            self.spot_track[cfg.code].append({"at": now.isoformat(), "spot": state.spot})
        self.sentiment_track[cfg.code].append(
            {"at": now.isoformat(), "bias": state.comp_bias, "components": dict(state.changes)}
        )

        self._manage_position(cfg.code, state, system_bias, now)

        if (
            state.spot is not None
            and state.call_wall is not None
            and state.put_floor is not None
            and not self.session_entries_blocked
            and in_execution_boundary(state.spot, state.call_wall, state.put_floor)
        ):
            logger.info(
                "[%s] monitoring active execution boundaries "
                "(spot=%.2f put=%d call=%d comp_bias=%s ai_bias=%s)",
                cfg.code,
                state.spot,
                state.put_floor,
                state.call_wall,
                state.comp_bias,
                system_bias,
            )

        if (
            not self.session_entries_blocked
            and state.position is None
            and state.trades_today < MAX_TRADES_PER_INDEX_PER_DAY
            and state.call_wall is not None
            and state.put_floor is not None
            and state.spot is not None
        ):
            self._check_entry(state, system_bias, now)

        self._publish_index_state(state, now)

    def _check_entry(self, state: IndexState, system_bias: str, now: datetime) -> None:
        candles = self.client.get_closed_5min_candles(state.config.spot_instrument_key)
        if candles is None:
            logger.warning(
                "[%s] legacy/historical candle keys detected; clearing index cache",
                state.config.code,
            )
            reset_index_live_cache(state)
            walls = self.client.get_oi_walls(state.config.spot_instrument_key, state.spot)
            if walls:
                state.call_wall, state.put_floor = walls
            return
        if not candles:
            return
        state.day_volume = sum(c["volume"] for c in candles)
        candle = candles[-1]
        if candle["timestamp"] == state.last_candle_ts:
            return
        state.last_candle_ts = candle["timestamp"]

        if state.call_wall is not None and state.put_floor is not None:
            direction = detect_setup(
                candle,
                state.call_wall,
                state.put_floor,
                state.comp_bias,
                system_bias,
            )
            if direction is not None:
                self._enter_trade(state, direction, candle["close"], now)

    def _enter_trade(self, state: IndexState, direction: str, entry: float, now: datetime) -> None:
        cfg = state.config
        if direction == "LONG":
            target, sl = entry + TARGET_POINTS, entry - STOP_LOSS_POINTS
        else:
            target, sl = entry - TARGET_POINTS, entry + STOP_LOSS_POINTS

        contract = self.client.get_itm_option_contract(cfg.spot_instrument_key, entry, direction)
        if contract is None:
            if not PAPER_TRADING:
                logger.error("[%s] could not resolve ITM contract; entry aborted", cfg.code)
                return
            desired = entry - ITM_OFFSET_POINTS if direction == "LONG" else entry + ITM_OFFSET_POINTS
            strike = int(round(desired / cfg.strike_step) * cfg.strike_step)
            contract = {
                "instrument_key": "",
                "strike": strike,
                "option_type": "CE" if direction == "LONG" else "PE",
            }

        quantity = cfg.lot_size * INITIAL_LOTS
        if not PAPER_TRADING:
            if not self.client.place_market_order(contract["instrument_key"], quantity, "BUY"):
                logger.error("[%s] entry order failed; trade not recorded", cfg.code)
                return
        else:
            logger.info(
                "[%s] PAPER %s entry @ %.2f via %d %s%s (2 lots)",
                cfg.code, direction, entry, contract["strike"], contract["option_type"],
                f" [{contract['instrument_key']}]" if contract['instrument_key'] else "",
            )

        state.position = Position(
            index_code=cfg.code,
            direction=direction,
            entry_price=entry,
            target_price=target,
            sl_price=sl,
            lot_size=cfg.lot_size,
            lots_remaining=INITIAL_LOTS,
            partial_booked=False,
            instrument_key=contract["instrument_key"] if not PAPER_TRADING else "",
            option_strike=int(contract["strike"]),
            option_type=str(contract["option_type"]),
            opened_at=now.isoformat(),
        )
        state.trades_today += 1
        self._record_event(
            now,
            cfg.code,
            "ENTRY",
            {
                "direction": direction,
                "entry_spot": entry,
                "target": target,
                "stop_loss": sl,
                "option": f"{contract['strike']}{contract['option_type']}",
                "instrument_key": contract["instrument_key"],
                "lots": INITIAL_LOTS,
                "quantity": quantity,
                "component_bias": state.comp_bias,
                "trade_number": state.trades_today,
            },
        )
        logger.info(
            "[%s] %s ENTRY @ %.2f via %d%s x%d (target %.2f / SL %.2f) trade %d/%d comp_bias=%s",
            cfg.code, direction, entry, contract["strike"], contract["option_type"], quantity,
            target, sl, state.trades_today, MAX_TRADES_PER_INDEX_PER_DAY, state.comp_bias,
        )
        telegram_notifier.notify_trade_execution(
            index_name=f"{cfg.display} ({contract['strike']}{contract['option_type']} x 2 lots)",
            trade_type=direction,
            entry_price=entry,
            target_price=target,
            sl_price=sl,
            component_sentiment=state.comp_bias,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _manage_position(
        self,
        index_code: str,
        state: IndexState,
        system_bias: str,
        now: datetime,
    ) -> None:
        """Manage open position for one index (partial book, target, stop-loss)."""
        pos = state.position
        if pos is None or state.spot is None:
            return
        spot = state.spot
        favorable = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)

        if not pos.partial_booked and pos.lots_remaining == INITIAL_LOTS:
            bias_opposed = (pos.direction == "LONG" and system_bias == "SHORT_ONLY") or (
                pos.direction == "SHORT" and system_bias == "LONG_ONLY"
            )
            if favorable >= PARTIAL_BOOK_POINTS:
                self._book_partial(index_code, state, spot, "HALF_TARGET_+60", now)
            elif bias_opposed and favorable > 0:
                self._book_partial(index_code, state, spot, f"BACKEND_BLOCKER_{system_bias}", now)
            pos = state.position
            if pos is None:
                return

        if pos.direction == "LONG":
            if spot >= pos.target_price:
                self._exit_position(index_code, state, spot, "TARGET", now)
            elif spot <= pos.sl_price:
                self._exit_position(index_code, state, spot, "STOP_LOSS", now)
        else:
            if spot <= pos.target_price:
                self._exit_position(index_code, state, spot, "TARGET", now)
            elif spot >= pos.sl_price:
                self._exit_position(index_code, state, spot, "STOP_LOSS", now)

    def _book_partial(
        self, index_code: str, state: IndexState, spot: float, reason: str, now: datetime
    ) -> None:
        """Fire a market order for 1 lot, leaving 1 lot to run to target/SL."""
        pos = state.position
        if pos is None or pos.partial_booked:
            return
        if pos.instrument_key:
            if not self.client.place_market_order(pos.instrument_key, pos.lot_size, "SELL"):
                logger.error("[%s] partial book order FAILED; will retry next tick", index_code)
                return
        pnl = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
        pos.lots_remaining -= 1
        pos.partial_booked = True
        self.realized_pnl_points[index_code] += pnl
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_AK07_OI,
            strategy_id="ak07_oi",
            symbol=index_code,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=spot,
            pnl_points=pnl,
            exit_reason=f"PARTIAL_BOOK — {reason}",
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
        )
        self._record_event(
            now,
            index_code,
            "PARTIAL_BOOK",
            {
                "direction": pos.direction,
                "spot": spot,
                "points": pnl,
                "lots_booked": 1,
                "lots_remaining": pos.lots_remaining,
                "reason": reason,
            },
        )
        logger.info(
            "[%s] PARTIAL BOOK 1 lot @ %.2f (%s, %+.2f pts); 1 lot runs to target/SL",
            index_code, spot, reason, pnl,
        )
        telegram_notifier.notify_trade_exit(
            index_name=f"{state.config.display} (1 of 2 lots)",
            trade_type=pos.direction,
            exit_price=spot,
            pnl_points=pnl,
            reason=f"PARTIAL BOOK - {reason}",
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _exit_position(
        self, index_code: str, state: IndexState, exit_price: float, reason: str, now: datetime
    ) -> None:
        pos = state.position
        if pos is None:
            return
        if pos.instrument_key:
            self.client.place_market_order(pos.instrument_key, pos.quantity, "SELL")
        pnl = (exit_price - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - exit_price)
        self.realized_pnl_points[index_code] += pnl * pos.lots_remaining
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_AK07_OI,
            strategy_id="ak07_oi",
            symbol=index_code,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl_points=pnl,
            exit_reason=reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
        )
        self._record_event(
            now,
            index_code,
            "EXIT",
            {
                "direction": pos.direction,
                "exit_spot": exit_price,
                "points": pnl,
                "lots_closed": pos.lots_remaining,
                "partial_was_booked": pos.partial_booked,
                "reason": reason,
                "option": f"{pos.option_strike}{pos.option_type}",
            },
        )
        logger.info(
            "[%s] %s EXIT @ %.2f (%s, %+.2f pts, %d lot(s))",
            index_code, pos.direction, exit_price, reason, pnl, pos.lots_remaining,
        )
        telegram_notifier.notify_trade_exit(
            index_name=f"{state.config.display} ({pos.lots_remaining} lot(s))",
            trade_type=pos.direction,
            exit_price=exit_price,
            pnl_points=pnl,
            reason=reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )
        state.position = None

    def square_off_all(self, reason: str) -> None:
        now = datetime.now(IST)
        for state in self.states.values():
            if state.position is not None:
                exit_price = state.spot if state.spot is not None else state.position.entry_price
                self._exit_position(state.config.code, state, exit_price, reason, now)

    def _record_event(self, now: datetime, index_code: str, event: str, detail: dict[str, Any]) -> None:
        entry = {"at": now.isoformat(), "index": index_code, "event": event, **detail}
        self.trade_log.append(entry)
        cache_manager.set_json(
            TRADE_LOG_KEY_TEMPLATE.format(day=now.date().isoformat()),
            self.trade_log,
            ttl_seconds=86_400,
        )

    def _maybe_archive_day(self, now: datetime) -> None:
        """15:30 IST post-market hook: dump the session to a performance JSON."""
        today = now.date().isoformat()
        if self.archived_day == today or now.time() < ARCHIVE_TIME:
            return
        self.archived_day = today
        try:
            payload = {
                "date": today,
                "generated_at": now.isoformat(),
                "paper_trading": PAPER_TRADING,
                "pnl_points_by_index": dict(self.realized_pnl_points),
                "pnl_points_total": round(sum(self.realized_pnl_points.values()), 2),
                "trade_log": self.trade_log,
                "spot_tracking": self.spot_track,
                "component_sentiment_tracking": self.sentiment_track,
                "redis_cache_dump": {
                    "live_state": cache_manager.get_market_snapshot(),
                    "system_mode": cache_manager.get_system_bias(),
                    "kill_switch": cache_manager.get_json(cache_manager.KILL_SWITCH_KEY),
                    "positions": cache_manager.get_json(cache_manager.POSITIONS_KEY),
                    "index_states": {
                        code: cache_manager.get_json(
                            cache_manager.INDEX_STATE_KEY_TEMPLATE.format(index=code)
                        )
                        for code in INDEX_CONFIGS
                    },
                },
            }
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            out_path = ARCHIVE_DIR / f"performance_review_{today}.json"
            out_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
            logger.info("Performance review archived: %s", out_path)
            performance_store.ingest_strategy1_trade_log(
                today,
                self.trade_log,
                paper_trading=PAPER_TRADING,
            )
            telegram_notifier.notify_system_event(
                "15:30 DATA ARCHIVAL",
                f"Session archived to {out_path.name} "
                f"(total {payload['pnl_points_total']:+.2f} pts, {len(self.trade_log)} events).",
            )
        except Exception as exc:
            logger.exception("Performance archival failed: %s", exc)

    def _roll_trade_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        for state in self.states.values():
            if state.trade_day != today:
                state.trade_day = today
                state.trades_today = 0
                state.last_candle_ts = ""
                self.trade_log = []
                self.spot_track = {c: [] for c in INDEX_CONFIGS}
                self.sentiment_track = {c: [] for c in INDEX_CONFIGS}
                self.realized_pnl_points = {c: 0.0 for c in INDEX_CONFIGS}

    def _kill_switch_engaged(self) -> bool:
        flag = cache_manager.get_json(cache_manager.KILL_SWITCH_KEY)
        return bool(flag and isinstance(flag, dict) and flag.get("engaged"))

    def _publish_index_state(self, state: IndexState, now: datetime) -> None:
        cfg = state.config
        cache_manager.set_json(
            cache_manager.INDEX_STATE_KEY_TEMPLATE.format(index=cfg.code),
            {
                "index": cfg.code,
                "display": cfg.display,
                "spot": state.spot,
                "call_wall": state.call_wall,
                "put_floor": state.put_floor,
                "component_bias": state.comp_bias,
                "components": state.changes,
                "trades_today": state.trades_today,
                "max_trades": MAX_TRADES_PER_INDEX_PER_DAY,
                "position": state.position.as_dict() if state.position else None,
                "entries_blocked": self.session_entries_blocked,
                "monitoring_active": (
                    not self.session_entries_blocked
                    and state.spot is not None
                    and state.call_wall is not None
                    and state.put_floor is not None
                    and in_execution_boundary(state.spot, state.call_wall, state.put_floor)
                ),
                "paper_trading": PAPER_TRADING,
                "updated_at": now.isoformat(),
            },
            ttl_seconds=120,
        )

    def _publish_global_state(self, now: datetime) -> None:
        positions = {
            code: s.position.as_dict() for code, s in self.states.items() if s.position is not None
        }
        cache_manager.set_json(cache_manager.POSITIONS_KEY, positions)
        cache_manager.set_json(
            cache_manager.ENGINE_HEARTBEAT_KEY,
            {"at": now.isoformat(), "paper_trading": PAPER_TRADING},
            ttl_seconds=60,
        )
        nifty = self.states["NIFTY"]
        if (
            nifty.spot is not None
            and nifty.call_wall is not None
            and nifty.put_floor is not None
        ):
            cache_manager.set_market_snapshot(
                {
                    "spot_price": float(nifty.spot),
                    "volume": int(nifty.day_volume),
                    "highest_call_oi_strike": int(nifty.call_wall),
                    "highest_put_oi_strike": int(nifty.put_floor),
                    "timestamp": now.isoformat(),
                }
            )


# ---------------------------------------------------------------------------
# Emergency square-off (callable from dashboard kill switch, engine-free)
# ---------------------------------------------------------------------------
def emergency_square_off_all() -> dict[str, str]:
    """Engage the kill switch and fire market square-offs for every open position."""
    now = datetime.now(IST).isoformat()
    cache_manager.set_json(
        cache_manager.KILL_SWITCH_KEY, {"engaged": True, "at": now, "source": "dashboard"}
    )
    results: dict[str, str] = {}
    positions = cache_manager.get_json(cache_manager.POSITIONS_KEY) or {}
    if not isinstance(positions, dict) or not positions:
        return {"status": "kill switch engaged; no open positions found"}

    client = UpstoxClient()
    for code, pos in positions.items():
        try:
            instrument = str(pos.get("instrument_key") or "")
            if not instrument:
                results[code] = "paper position flagged for engine square-off"
                continue
            ok = client.place_market_order(instrument, int(pos.get("quantity", 0)), "SELL")
            results[code] = "square-off order sent" if ok else "ORDER FAILED - check broker"
        except Exception as exc:
            logger.exception("Emergency square-off failed for %s: %s", code, exc)
            results[code] = "ERROR - see engine logs"
    telegram_notifier.notify_system_event(
        "EMERGENCY KILL SWITCH", f"Cockpit kill switch engaged at {now}. Results: {results}"
    )
    return results


def release_kill_switch() -> None:
    cache_manager.delete_key(cache_manager.KILL_SWITCH_KEY)
    logger.info("Kill switch released")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    AK07Engine().run()


if __name__ == "__main__":
    main()
