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
  the partial target (Nifty/BN +30 · Sensex +60), run the rest to the full
  target (Nifty/BN +60 · Sensex +120) or the index stop (Nifty/BN -30 · Sensex -60).
- Daily session automation: at AK07_TOKEN_REFRESH_IST (default 06:00 IST)
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
# Strategy constants — per-index spot risk (2 lots: book 1 at partial, exit 2 at target)
# ---------------------------------------------------------------------------
INDEX_OI_RISK: Final[dict[str, tuple[float, float, float]]] = {
    # index: (stop_loss_pts, partial_book_pts, full_target_pts)
    "NIFTY": (
        float(os.environ.get("AK07_OI_SL_PTS_NIFTY", "30")),
        float(os.environ.get("AK07_OI_PARTIAL_PTS_NIFTY", "30")),
        float(os.environ.get("AK07_OI_TARGET_PTS_NIFTY", "60")),
    ),
    "BANKNIFTY": (
        float(os.environ.get("AK07_OI_SL_PTS_BANKNIFTY", "30")),
        float(os.environ.get("AK07_OI_PARTIAL_PTS_BANKNIFTY", "30")),
        float(os.environ.get("AK07_OI_TARGET_PTS_BANKNIFTY", "60")),
    ),
    "SENSEX": (
        float(os.environ.get("AK07_OI_SL_PTS_SENSEX", "60")),
        float(os.environ.get("AK07_OI_PARTIAL_PTS_SENSEX", "60")),
        float(os.environ.get("AK07_OI_TARGET_PTS_SENSEX", "120")),
    ),
}
DEFAULT_OI_RISK: Final[tuple[float, float, float]] = (60.0, 60.0, 120.0)
INITIAL_LOTS: Final[int] = 2
ITM_OFFSET_POINTS: Final[float] = 50.0  # legacy fallback when chain/greeks unavailable
# Spot-aligned option pick: target |delta| so premium tracks Nifty (ATM ~0.5, mild ITM ~0.55–0.65).
OPTION_TARGET_DELTA: Final[float] = float(os.environ.get("AK07_OPTION_TARGET_DELTA", "0.60"))
OPTION_DELTA_MIN: Final[float] = float(os.environ.get("AK07_OPTION_DELTA_MIN", "0.40"))
OPTION_DELTA_MAX: Final[float] = float(os.environ.get("AK07_OPTION_DELTA_MAX", "0.75"))
MAX_TRADES_PER_INDEX_PER_DAY: Final[int] = 2
SUPPORT_POCKET_POINTS: Final[float] = float(os.environ.get("AK07_SUPPORT_POCKET_PTS", "20"))
RESISTANCE_POCKET_POINTS: Final[float] = float(os.environ.get("AK07_RESISTANCE_POCKET_PTS", "20"))
WICK_REJECTION_RATIO: Final[float] = float(os.environ.get("AK07_WICK_REJECTION_RATIO", "0.50"))
OI_VELOCITY_GATE_ENABLED: Final[bool] = os.environ.get("AK07_OI_VELOCITY_GATE", "0").strip().lower() in (
    "1", "true", "yes",
)
OI_PCR_GATE_ENABLED: Final[bool] = os.environ.get("AK07_OI_PCR_GATE", "0").strip().lower() in (
    "1", "true", "yes",
)
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
NO_ENTRY_AFTER: Final[dtime] = _parse_ist_time("AK07_NO_ENTRY_AFTER_IST", 14, 45)
TOKEN_REFRESH_TIME: Final[dtime] = _parse_ist_time("AK07_TOKEN_REFRESH_IST", 6, 0)
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
        lot_size=int(os.environ.get("AK07_LOT_SIZE_NIFTY", "65")),
        strike_step=50,
    ),
    "BANKNIFTY": IndexConfig(
        code="BANKNIFTY",
        display="BankNifty",
        spot_instrument_key="NSE_INDEX|Nifty Bank",
        heavyweights=("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"),
        lot_size=int(os.environ.get("AK07_LOT_SIZE_BANKNIFTY", "30")),
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


def _format_future_contract_label(trading_symbol: str, expiry: str) -> str:
    if expiry:
        try:
            exp_date = date.fromisoformat(expiry[:10])
            return f"{trading_symbol} FUT {exp_date.strftime('%d %b')}"
        except ValueError:
            pass
    return f"{trading_symbol} FUT"


_INDEX_FUTURE_SEGMENT: Final[dict[str, str]] = {
    "NIFTY": "NSE_FO",
    "BANKNIFTY": "NSE_FO",
    "SENSEX": "BSE_FO",
}

_INDEX_FUTURE_SYMBOL_PREFIX: Final[dict[str, str]] = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "SENSEX": "SENSEX",
}


def _row_is_index_future(row: dict[str, Any], index_code: str) -> bool:
    """True for NIFTY/BANKNIFTY/SENSEX index futures rows (not options)."""
    code = index_code.upper()
    want_seg = _INDEX_FUTURE_SEGMENT.get(code)
    if not want_seg:
        return False
    if str(row.get("segment") or "") != want_seg:
        return False
    inst = str(row.get("instrument_type") or "").upper()
    if inst in ("CE", "PE", "OPTIDX", "OPTSTK"):
        return False
    sym = str(row.get("trading_symbol") or "").upper()
    prefix = _INDEX_FUTURE_SYMBOL_PREFIX.get(code, code)
    if not sym.startswith(prefix):
        return False
    if code == "NIFTY" and ("NXT" in sym or sym.startswith("FINNIFTY") or sym.startswith("MIDCPNIFTY")):
        return False
    if sym.endswith("CE") or sym.endswith("PE"):
        return False
    return bool(str(row.get("instrument_key") or ""))


def _future_expiry_key(row: dict[str, Any]) -> str:
    raw = row.get("expiry")
    if raw is None:
        return "9999-99-99"
    if isinstance(raw, (int, float)):
        return str(int(raw))
    text = str(raw).strip()
    return text[:10] if text else "9999-99-99"


def _index_futures_from_master(index_code: str) -> list[dict[str, Any]]:
    """Fallback: scan Upstox complete.json.gz for index futures."""
    import gzip
    import json

    from app.services.instrument_catalog import CACHE_DIR, COMPLETE_URL, GZ_FILE  # noqa: PLC0415

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if not GZ_FILE.exists():
            logger.info("Downloading Upstox instrument master for %s futures lookup…", index_code)
            response = requests.get(COMPLETE_URL, timeout=120)
            response.raise_for_status()
            GZ_FILE.write_bytes(response.content)
        with gzip.open(GZ_FILE, "rt", encoding="utf-8") as handle:
            rows = json.load(handle)
    except (OSError, ValueError, requests.RequestException) as exc:
        logger.warning("Instrument master scan failed for %s: %s", index_code, exc)
        return []
    return [row for row in rows if isinstance(row, dict) and _row_is_index_future(row, index_code)]


def _instrument_keys_match(expected: str, actual: str) -> bool:
    """Match Upstox instrument ids across chain (NSE_FO|token) vs positions (token-only)."""
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    expected_suffix = expected.split("|", 1)[-1]
    actual_suffix = actual.split("|", 1)[-1]
    return expected_suffix == actual or actual_suffix == expected or expected_suffix == actual_suffix


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
        from broker_http import session_for_user

        self.session = session_for_user(username)
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
            response = self.session.post(
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
            if response.status_code == 200:
                notifier_url = str(((body.get("data") or {}).get("notifier_url")) or "")
                logger.info(
                    "Upstox V3 token request accepted — approve in Upstox app/WhatsApp; "
                    "token will POST to notifier%s",
                    f" ({notifier_url})" if notifier_url else "",
                )
                return True
            error_code = ""
            errors = body.get("errors") if isinstance(body.get("errors"), list) else []
            if errors and isinstance(errors[0], dict):
                error_code = str(errors[0].get("errorCode") or errors[0].get("error_code") or "")
            logger.error(
                "Upstox V3 token request failed: HTTP %d %s",
                response.status_code,
                str(response.text or "")[:300],
            )
            if error_code == "UDAPI1123":
                logger.error(
                    "UDAPI1123 = Upstox rejected the Notifier URL saved in My Apps. "
                    "Set exactly https://ak07.in/api/upstox/token-notifier (HTTPS, no trailing slash), "
                    "verify `curl -s https://ak07.in/api/upstox/token-notifier` returns JSON, "
                    "ensure nginx proxies /api/ to port 8080, then Edit+Save the app in Upstox portal."
                )
            return False
        except Exception as exc:  # Bound exception explicitly for debugging safely
            logger.exception("Unexpected failure during daily token refresh: %s", exc)
            return False

    # Module-level 429 cool-down shared across clients (Upstox rate limit is account-wide).
    _ltp_cooldown_until: float = 0.0
    # Short-lived LTP cache — many engines ask the same keys every few seconds.
    _ltp_cache: dict[str, tuple[float, float]] = {}  # key -> (price, mono_expiry)
    _ltp_cache_ttl_sec: float = float(os.environ.get("AK07_LTP_CACHE_SEC", "20"))
    # Day open is static after auction — cache per instrument for the IST calendar day.
    _day_open_cache: dict[str, tuple[float, date]] = {}
    # After a failed day-OHLC fetch, wait before retrying (avoids 429 thrash every poll).
    _day_open_retry_after: dict[str, float] = {}

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        now = time.monotonic()
        if now < UpstoxClient._ltp_cooldown_until and "market-quote" in url:
            return None
        try:
            response = self.session.get(url, params=params, timeout=15)
            if response.status_code == 429:
                # Back off hard — hammering LTP/OHLC prevents option SL/TP/trail forever.
                # 3 minutes: account quota often stays hot after gamma/multi-engine bursts.
                UpstoxClient._ltp_cooldown_until = time.monotonic() + 180.0
                logger.warning(
                    "Upstox HTTP 429 on %s — cooling market-quote for 180s",
                    url.split("?")[0],
                )
                return None
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
        now = time.monotonic()
        cached = UpstoxClient._ltp_cache.get(instrument_key)
        if cached is not None and cached[1] > now:
            return cached[0]

        data = self._get(f"{self.base_url}/market-quote/ltp", {"instrument_key": instrument_key})
        if not isinstance(data, dict):
            # Serve stale cache during cool-down / 429 so option trail can still evaluate.
            if cached is not None:
                return cached[0]
            return None
        for row in data.values():
            ltp = row.get("last_price")
            if ltp is not None:
                value = float(ltp)
                ttl = max(5.0, UpstoxClient._ltp_cache_ttl_sec)
                UpstoxClient._ltp_cache[instrument_key] = (value, now + ttl)
                return value
        if cached is not None:
            return cached[0]
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
            key = row.get("instrument_key") or row.get("instrument_token") or ""
            ltp = row.get("last_price")
            day_open = (row.get("ohlc") or {}).get("open")
            if key and ltp is not None and day_open:
                out[key] = {"ltp": float(ltp), "open": float(day_open)}
        return out

    def get_index_day_open(self, instrument_key: str) -> float | None:
        """NSE session open from daily OHLC quote (often matches TradingView index open).

        Cached for the IST calendar day once known — value does not change intraday, and
        re-hitting /market-quote/ohlc every poll burns the shared Upstox rate limit (429).
        """
        today = datetime.now(IST).date()
        cached = UpstoxClient._day_open_cache.get(instrument_key)
        if cached is not None and cached[1] == today:
            return cached[0]

        now = time.monotonic()
        if now < UpstoxClient._day_open_retry_after.get(instrument_key, 0.0):
            return None

        data = self._get(
            f"{self.base_url}/market-quote/ohlc",
            {"instrument_key": instrument_key, "interval": "1d"},
        )
        if not isinstance(data, dict):
            # Miss / 429 / cool-down: back off so breakout poll does not thrash OHLC.
            UpstoxClient._day_open_retry_after[instrument_key] = now + 120.0
            return None
        for row in data.values():
            day_open = (row.get("ohlc") or {}).get("open")
            if day_open is not None:
                value = float(day_open)
                UpstoxClient._day_open_cache[instrument_key] = (value, today)
                UpstoxClient._day_open_retry_after.pop(instrument_key, None)
                return value
        UpstoxClient._day_open_retry_after[instrument_key] = now + 120.0
        return None

    def get_closed_5min_candles(self, instrument_key: str) -> list[dict[str, float]] | None:
        """Today's completed 5-min candles from V3 intraday. None = legacy payload rejected."""
        v3_base = self.base_url.replace("/v2", "/v3")
        data = self._get(
            f"{v3_base}/historical-candle/intraday/{instrument_key}/minutes/{CANDLE_MINUTES}"
        )
        return parse_v3_intraday_candles(data, datetime.now(IST))

    def _nearest_expiry(self, instrument_key: str) -> str | None:
        expiries = self.list_expiries(instrument_key)
        return expiries[0] if expiries else None

    def list_expiries(self, instrument_key: str) -> list[str]:
        data = self._get(f"{self.base_url}/option/contract", {"instrument_key": instrument_key})
        if not isinstance(data, list):
            return []
        today = date.today().isoformat()
        return sorted({str(c.get("expiry")) for c in data if str(c.get("expiry", "")) >= today})

    def get_option_chain_for_expiry(self, instrument_key: str, expiry_date: str) -> list[dict[str, Any]]:
        data = self._get(
            f"{self.base_url}/option/chain",
            {"instrument_key": instrument_key, "expiry_date": expiry_date},
        )
        return data if isinstance(data, list) else []

    def resolve_expiry_on_date(self, instrument_key: str, on_date: date) -> str | None:
        """Pick chain expiry matching calendar day (or nearest same-week)."""
        target = on_date.isoformat()
        expiries = self.list_expiries(instrument_key)
        if target in expiries:
            return target
        for exp in expiries:
            if exp.startswith(target[:7]):
                return exp
        return expiries[0] if expiries else None

    def _fetch_nearest_expiry_chain(self, instrument_key: str) -> list[dict[str, Any]]:
        """Rows of the active weekly (nearest-expiry) option chain."""
        expiry = self._nearest_expiry(instrument_key)
        if not expiry:
            return []
        return self.get_option_chain_for_expiry(instrument_key, expiry)

    def get_itm_option_contract(
        self, instrument_key: str, spot: float, direction: str
    ) -> dict[str, Any] | None:
        """ATM / mild-ITM option whose |delta| best tracks spot (greeks-aware).

        LONG → CE near ATM–1 ITM with |delta|≈0.55.
        SHORT → PE near ATM–1 ITM with |delta|≈0.55.
        Falls back to fixed ITM offset when chain/greeks unavailable.
        """
        from app.services.options_greeks import pick_spot_aligned_option

        expiry = self._nearest_expiry(instrument_key)
        rows = self.get_option_chain_for_expiry(instrument_key, expiry) if expiry else []
        code = _index_code_for_spot_key(instrument_key)
        step = INDEX_CONFIGS[code].strike_step if code and code in INDEX_CONFIGS else 50
        if expiry and rows:
            picked = pick_spot_aligned_option(
                spot=spot,
                chain_rows=rows,
                expiry=expiry,
                direction=direction,
                strike_step=step,
                target_delta=OPTION_TARGET_DELTA,
                delta_min=OPTION_DELTA_MIN,
                delta_max=OPTION_DELTA_MAX,
            )
            if picked and picked.get("instrument_key"):
                logger.info(
                    "Option pick %s %s%d delta=%s selection=%s (spot=%.2f target_δ=%.2f)",
                    direction,
                    picked.get("option_type"),
                    picked.get("strike"),
                    picked.get("abs_delta") or picked.get("delta"),
                    picked.get("selection"),
                    spot,
                    OPTION_TARGET_DELTA,
                )
                return {
                    "instrument_key": str(picked["instrument_key"]),
                    "strike": int(picked["strike"]),
                    "option_type": str(picked["option_type"]),
                    "delta": picked.get("delta"),
                    "abs_delta": picked.get("abs_delta"),
                    "selection": picked.get("selection"),
                    "expiry": picked.get("expiry") or expiry,
                }

        # Legacy geometric ITM (~50 pts)
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
                    "selection": "itm_offset_fallback",
                    "expiry": expiry,
                }
        return best

    def get_index_future_contract(self, index_code: str) -> dict[str, Any] | None:
        """Nearest-expiry index futures contract (NSE FUTIDX / BSE)."""
        cfg = INDEX_CONFIGS.get(index_code.upper())
        if not cfg:
            return None
        code = cfg.code
        exchange = "BSE" if code == "SENSEX" else "NSE"
        today = date.today().isoformat()

        search_specs: list[dict[str, Any]] = [
            {"query": code, "exchanges": exchange, "segments": "FO", "expiry": "current_month", "records": 30},
            {"query": code, "exchanges": exchange, "segments": "FO", "expiry": "near_month", "records": 30},
            {"query": code, "exchanges": exchange, "segments": "FO", "records": 30},
        ]

        seen_keys: set[str] = set()
        merged: list[dict[str, Any]] = []
        for params in search_specs:
            data = self._get(f"{self.base_url}/instruments/search", params)
            if not isinstance(data, list):
                continue
            for row in data:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("instrument_key") or "")
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                merged.append(row)

        if not merged:
            merged = _index_futures_from_master(code)

        candidates: list[tuple[str, dict[str, Any]]] = []
        for row in merged:
            if not _row_is_index_future(row, code):
                continue
            expiry = _future_expiry_key(row)
            if expiry[:10] < today:
                continue
            candidates.append((expiry, row))

        if not candidates:
            logger.warning(
                "No %s future contract found (search rows=%d, after filter=0)",
                code,
                len(merged),
            )
            return None

        candidates.sort(key=lambda item: item[0])
        row = candidates[0][1]
        trading_symbol = str(row.get("trading_symbol") or code)
        expiry = _future_expiry_key(row)[:10]
        return {
            "instrument_key": str(row.get("instrument_key") or ""),
            "trading_symbol": trading_symbol,
            "expiry": expiry,
            "contract_label": _format_future_contract_label(code, expiry),
        }

    def get_option_chain_with_expiry(self, instrument_key: str) -> tuple[str | None, list[dict[str, Any]]]:
        """Nearest weekly expiry label and full chain rows."""
        expiry = self._nearest_expiry(instrument_key)
        if not expiry:
            return None, []
        return expiry, self._fetch_nearest_expiry_chain(instrument_key)

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

    def get_oi_extended(self, instrument_key: str, spot: float | None = None) -> dict | None:
        """Extended OI scan: walls + PCR + intraday velocity hotspots.

        Returns a dict with keys:
          call_wall, put_floor, pcr,
          max_ce_writing_strike (highest intraday CE OI change),
          max_pe_writing_strike (highest intraday PE OI change)

        These additional signals align with the AK07 OI Scanner pattern:
          - PCR < 0.75 → bearish regime (prefer SHORT)
          - PCR > 1.25 → bullish regime (prefer LONG)
          - max_ce_writing_strike == call_wall → wall is actively defended (SHORT confirmation)
          - max_pe_writing_strike == put_floor  → floor is actively defended (LONG confirmation)
        """
        data = self._fetch_nearest_expiry_chain(instrument_key)
        if not data:
            return None

        total_ce_oi = 0.0
        total_pe_oi = 0.0
        best_call_oi: tuple[float, int] | None = None
        best_put_oi: tuple[float, int] | None = None
        best_ce_chg: tuple[float, int] | None = None
        best_pe_chg: tuple[float, int] | None = None

        for row in data:
            try:
                strike = int(float(row.get("strike_price", 0)))
            except (TypeError, ValueError):
                continue
            if spot is not None and abs(strike - spot) > OI_BAND_POINTS:
                continue
            call_md = ((row.get("call_options") or {}).get("market_data") or {})
            put_md  = ((row.get("put_options")  or {}).get("market_data") or {})
            c_oi  = float(call_md.get("oi") or 0)
            p_oi  = float(put_md.get("oi")  or 0)
            c_chg = float(call_md.get("oi_change") or 0)
            p_chg = float(put_md.get("oi_change")  or 0)
            total_ce_oi += c_oi
            total_pe_oi += p_oi
            if best_call_oi is None or c_oi > best_call_oi[0]:
                best_call_oi = (c_oi, strike)
            if best_put_oi is None or p_oi > best_put_oi[0]:
                best_put_oi = (p_oi, strike)
            if best_ce_chg is None or c_chg > best_ce_chg[0]:
                best_ce_chg = (c_chg, strike)
            if best_pe_chg is None or p_chg > best_pe_chg[0]:
                best_pe_chg = (p_chg, strike)

        if not best_call_oi or not best_put_oi:
            return None
        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0.0
        return {
            "call_wall": best_call_oi[1],
            "put_floor": best_put_oi[1],
            "pcr": pcr,
            "max_ce_writing_strike": best_ce_chg[1] if best_ce_chg else None,
            "max_pe_writing_strike": best_pe_chg[1] if best_pe_chg else None,
        }

    def place_market_order(
        self,
        instrument_key: str,
        quantity: int,
        transaction_type: str,
        *,
        bypass_profit_guard: bool = False,
    ) -> bool:
        """Market order via the standard then HFT endpoint."""
        if (
            not bypass_profit_guard
            and transaction_type.upper() == "BUY"
            and not PAPER_TRADING
        ):
            from app.services.daily_profit_guard import profit_target_engaged  # noqa: PLC0415

            if profit_target_engaged():
                logger.warning(
                    "BUY blocked — AK07 daily target hit (%s x %d); signal-only mode",
                    instrument_key,
                    quantity,
                )
                return False

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
                    if transaction_type.upper() == "BUY" and not bypass_profit_guard:
                        from app.services.daily_profit_guard import record_broker_entry  # noqa: PLC0415

                        record_broker_entry()
                    return True
                logger.warning(
                    "Order rejected at %s: HTTP %d %s", url, response.status_code, str(body)[:250]
                )
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Order error at %s: %s", url, exc)
        logger.error("Order FAILED on all endpoints: %s %d x %s", transaction_type, quantity, instrument_key)
        return False

    def get_net_position_qty(self, instrument_key: str) -> int | None:
        """Net intraday qty for one instrument. 0 = flat at broker. None = API failed or unknown."""
        if not instrument_key:
            return None
        data = self._get(f"{self.base_url}/portfolio/short-term-positions")
        if not isinstance(data, list):
            return None
        for row in data:
            if not isinstance(row, dict):
                continue
            key = str(row.get("instrument_token") or row.get("instrument_key") or "")
            if not _instrument_keys_match(instrument_key, key):
                continue
            try:
                return int(row.get("quantity") or 0)
            except (TypeError, ValueError):
                return 0
        return None

    def get_short_term_positions(self) -> list[dict[str, Any]]:
        data = self._get(f"{self.base_url}/portfolio/short-term-positions")
        return data if isinstance(data, list) else []

    def get_portfolio_day_pnl(self) -> dict[str, float] | None:
        data = self._get(f"{self.base_url}/portfolio/short-term-positions")
        if not isinstance(data, list):
            return None
        total_pnl = realised = unrealised = 0.0
        open_positions = 0
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                qty = int(row.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty != 0:
                open_positions += 1
            total_pnl += float(row.get("pnl") or 0.0)
            realised += float(row.get("realised") or 0.0)
            unrealised += float(row.get("unrealised") or 0.0)
        return {
            "total_pnl": total_pnl,
            "realised": realised,
            "unrealised": unrealised,
            "open_positions": float(open_positions),
        }

    def square_off_all_open_positions(self, *, bypass_profit_guard: bool = True) -> list[dict[str, Any]]:
        """Market-exit every non-flat short-term position."""
        results: list[dict[str, Any]] = []
        for row in self.get_short_term_positions():
            if not isinstance(row, dict):
                continue
            try:
                qty = int(row.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue
            key = str(row.get("instrument_token") or row.get("instrument_key") or "")
            symbol = str(row.get("trading_symbol") or row.get("symbol") or key)
            side = "SELL" if qty > 0 else "BUY"
            ok = self.place_market_order(
                key,
                abs(qty),
                side,
                bypass_profit_guard=bypass_profit_guard,
            )
            results.append(
                {
                    "instrument": key,
                    "symbol": symbol,
                    "qty": abs(qty),
                    "side": side,
                    "status": "square-off sent" if ok else "ORDER FAILED",
                }
            )
        return results


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

    def get_oi_extended(self, instrument_key: str, spot: float | None = None) -> dict | None:
        walls = self.get_oi_walls(instrument_key, spot)
        if not walls:
            return None
        return {
            "call_wall": walls[0],
            "put_floor": walls[1],
            "pcr": None,
            "max_ce_writing_strike": None,
            "max_pe_writing_strike": None,
        }

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

    def get_index_future_contract(self, index_code: str) -> dict[str, Any] | None:
        code = index_code.upper()
        if code not in INDEX_CONFIGS:
            return None
        return {
            "instrument_key": "",
            "trading_symbol": code,
            "expiry": "",
            "contract_label": _format_future_contract_label(code, ""),
        }

    def place_market_order(
        self,
        instrument_key: str,
        quantity: int,
        transaction_type: str,
        *,
        bypass_profit_guard: bool = False,
    ) -> bool:
        logger.info("MOCK order: %s %d x %s", transaction_type, quantity, instrument_key or "paper")
        return True

    def get_net_position_qty(self, instrument_key: str) -> int | None:
        return None

    def get_portfolio_day_pnl(self) -> dict[str, float] | None:
        return {"total_pnl": 0.0, "realised": 0.0, "unrealised": 0.0, "open_positions": 0.0}

    def square_off_all_open_positions(self, *, bypass_profit_guard: bool = True) -> list[dict[str, Any]]:
        return []

    def advance_tick(self) -> None:
        self._tick += 1


def _index_code_for_spot_key(instrument_key: str) -> str | None:
    for code, cfg in INDEX_CONFIGS.items():
        if cfg.spot_instrument_key == instrument_key:
            return code
    return None


def build_upstox_client(username: str = "AK07") -> UpstoxClient:
    if MOCK_MODE:
        logger.info("AK07_MOCK=1 -> using MockUpstoxClient (simulated V3 feed)")
        return MockUpstoxClient()
    return UpstoxClient(username=username)


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
    pcr: float | None = None,
    max_ce_writing_strike: int | None = None,
    max_pe_writing_strike: int | None = None,
) -> str | None:
    """Evaluate SETUP 1 (LONG) and SETUP 2 (SHORT) on a closed 5-min candle.

    Enhanced with optional gates (env toggles, off by default):
      1. PCR regime filter (AK07_OI_PCR_GATE=1): skip LONG when PCR < 0.75, SHORT when PCR > 1.25.
      2. OI velocity confirmation (AK07_OI_VELOCITY_GATE=1): require max writing strike == wall/floor.
         Disabled by default — intraday velocity hotspots rarely align with max-total-OI walls.
    """
    spot = candle["close"]

    # PCR regime thresholds (optional — AK07_OI_PCR_GATE=1)
    pcr_bearish = OI_PCR_GATE_ENABLED and pcr is not None and pcr < 0.75
    pcr_bullish = OI_PCR_GATE_ENABLED and pcr is not None and pcr > 1.25

    in_support_pocket = put_floor <= spot <= put_floor + SUPPORT_POCKET_POINTS
    # Velocity gate (optional — AK07_OI_VELOCITY_GATE=1): exact strike match rarely aligns
    # with max-total-OI walls; pocket + wick logic is the primary entry filter.
    if OI_VELOCITY_GATE_ENABLED:
        floor_velocity_ok = max_pe_writing_strike is None or max_pe_writing_strike == put_floor
    else:
        floor_velocity_ok = True
    if (
        in_support_pocket
        and lower_wick_ratio(candle) >= WICK_REJECTION_RATIO
        and comp_bias in ("BULLISH", "NEUTRAL")
        and system_bias != "SHORT_ONLY"
        and not pcr_bearish
        and floor_velocity_ok
    ):
        return "LONG"

    in_resistance_pocket = call_wall - RESISTANCE_POCKET_POINTS <= spot <= call_wall
    if OI_VELOCITY_GATE_ENABLED:
        wall_velocity_ok = max_ce_writing_strike is None or max_ce_writing_strike == call_wall
    else:
        wall_velocity_ok = True
    if (
        in_resistance_pocket
        and upper_wick_ratio(candle) >= WICK_REJECTION_RATIO
        and comp_bias in ("BEARISH", "NEUTRAL")
        and system_bias != "LONG_ONLY"
        and not pcr_bullish
        and wall_velocity_ok
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
    # AK07 OI Scanner extensions: PCR regime + intraday OI velocity
    pcr: float | None = None
    max_ce_writing_strike: int | None = None
    max_pe_writing_strike: int | None = None


def reset_index_live_cache(state: IndexState) -> None:
    """Drop stale in-memory fields for one index."""
    state.spot = None
    state.call_wall = None
    state.put_floor = None
    state.walls_refreshed_at = 0.0
    state.changes = {}
    state.comp_bias = "NEUTRAL"
    state.last_candle_ts = ""
    state.pcr = None
    state.max_ce_writing_strike = None
    state.max_pe_writing_strike = None


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
        logger.info("AK07 archive directory: %s", ARCHIVE_DIR.resolve())
        migrated = performance_store.migrate_legacy_archives_to_volume(ingest_redis=True)
        if migrated.get("copied"):
            logger.info("Migrated legacy archives on startup: %s", migrated["copied"])
        self._restore_session_from_redis(datetime.now(IST))
        for code, risk in INDEX_OI_RISK.items():
            sl, partial, target = risk
            logger.info(
                "[%s] OI risk — SL %.0f pts · partial +%.0f · target +%.0f (2 lots)",
                code,
                sl,
                partial,
                target,
            )
        logger.info("AK07 engine initialized (paper_trading=%s)", PAPER_TRADING)

    def _restore_session_from_redis(self, now: datetime) -> None:
        """Survive container restarts: reload today's trade log and per-index entry counts."""
        today = now.date().isoformat()
        for state in self.states.values():
            state.trade_day = today

        cached = cache_manager.get_json(cache_manager.TRADE_LOG_KEY_TEMPLATE.format(day=today))
        if not isinstance(cached, list) or not cached:
            return

        self.trade_log = cached
        entry_counts = {code: 0 for code in INDEX_CONFIGS}
        pnl_by_index = {code: 0.0 for code in INDEX_CONFIGS}

        for event in self.trade_log:
            if not isinstance(event, dict):
                continue
            idx = str(event.get("index") or "")
            if idx not in entry_counts:
                continue
            kind = str(event.get("event") or "")
            if kind == "ENTRY":
                entry_counts[idx] += 1
            elif kind in ("PARTIAL_BOOK", "EXIT"):
                try:
                    pnl_by_index[idx] += float(event.get("points") or 0)
                except (TypeError, ValueError):
                    pass

        for code, count in entry_counts.items():
            self.states[code].trades_today = count
            self.realized_pnl_points[code] = pnl_by_index[code]

        positions = cache_manager.get_json(cache_manager.POSITIONS_KEY)
        if isinstance(positions, dict):
            for code, raw in positions.items():
                state = self.states.get(code)
                if state is None or not isinstance(raw, dict):
                    continue
                try:
                    state.position = Position(
                        index_code=code,
                        direction=str(raw["direction"]),
                        entry_price=float(raw["entry_price"]),
                        target_price=float(raw["target_price"]),
                        sl_price=float(raw["sl_price"]),
                        lot_size=int(raw["lot_size"]),
                        lots_remaining=int(raw.get("lots_remaining") or INITIAL_LOTS),
                        partial_booked=bool(raw.get("partial_booked")),
                        instrument_key=str(raw.get("instrument_key") or ""),
                        option_strike=int(raw["option_strike"]),
                        option_type=str(raw["option_type"]),
                        opened_at=str(raw.get("opened_at") or ""),
                    )
                    logger.info(
                        "[%s] restored open position from Redis (%s @ %.2f · SL %.2f · T %.2f · %d lot(s))",
                        code,
                        state.position.direction,
                        state.position.entry_price,
                        state.position.sl_price,
                        state.position.target_price,
                        state.position.lots_remaining,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("[%s] could not restore position from Redis: %s", code, exc)

        logger.info(
            "Restored session from Redis: %d trade_log events (entries %s)",
            len(self.trade_log),
            ", ".join(f"{c}={entry_counts[c]}" for c in INDEX_CONFIGS),
        )

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
        elif now.time() >= NO_ENTRY_AFTER:
            self.session_entries_blocked = True
            logger.debug("entries blocked (post %s IST no-entry window)", NO_ENTRY_AFTER.strftime("%H:%M"))
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
                ext = self.client.get_oi_extended(cfg.spot_instrument_key, state.spot)
                if ext:
                    state.call_wall = ext["call_wall"]
                    state.put_floor = ext["put_floor"]
                    state.pcr = ext["pcr"]
                    state.max_ce_writing_strike = ext["max_ce_writing_strike"]
                    state.max_pe_writing_strike = ext["max_pe_writing_strike"]
                    logger.info(
                        "[%s] walls refreshed call=%d put=%d PCR=%.2f ce_velocity=%s pe_velocity=%s",
                        cfg.code,
                        state.call_wall,
                        state.put_floor,
                        state.pcr or 0.0,
                        state.max_ce_writing_strike,
                        state.max_pe_writing_strike,
                    )
                state.walls_refreshed_at = time.monotonic()
        except Exception as exc:
            logger.exception("[%s] live V3 feed read failed; resetting index cache: %s", cfg.code, exc)
            reset_index_live_cache(state)
            ext = self.client.get_oi_extended(cfg.spot_instrument_key, state.spot)
            if ext:
                state.call_wall = ext["call_wall"]
                state.put_floor = ext["put_floor"]
                state.pcr = ext["pcr"]
                state.max_ce_writing_strike = ext["max_ce_writing_strike"]
                state.max_pe_writing_strike = ext["max_pe_writing_strike"]
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
            ext = self.client.get_oi_extended(state.config.spot_instrument_key, state.spot)
            if ext:
                state.call_wall = ext["call_wall"]
                state.put_floor = ext["put_floor"]
                state.pcr = ext["pcr"]
                state.max_ce_writing_strike = ext["max_ce_writing_strike"]
                state.max_pe_writing_strike = ext["max_pe_writing_strike"]
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
                pcr=state.pcr,
                max_ce_writing_strike=state.max_ce_writing_strike,
                max_pe_writing_strike=state.max_pe_writing_strike,
            )
            if direction is not None:
                self._enter_trade(state, direction, candle["close"], now)

    def _enter_trade(self, state: IndexState, direction: str, entry: float, now: datetime) -> None:
        cfg = state.config
        sl_pts, _, target_pts = INDEX_OI_RISK.get(cfg.code, DEFAULT_OI_RISK)
        if direction == "LONG":
            target, sl = entry + target_pts, entry - sl_pts
        else:
            target, sl = entry - target_pts, entry + sl_pts

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
        _ = system_bias  # entry gating only; partial book uses fixed index points
        pos = state.position
        if pos is None or state.spot is None:
            return
        spot = state.spot
        favorable = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)

        # Stop-loss always checked first (all lots still open).
        if pos.direction == "LONG":
            if spot <= pos.sl_price:
                self._exit_position(index_code, state, spot, "STOP_LOSS", now)
                return
        elif spot >= pos.sl_price:
            self._exit_position(index_code, state, spot, "STOP_LOSS", now)
            return

        # Partial book 1 of 2 lots at fixed index points (no early AI-bias partial).
        if not pos.partial_booked and pos.lots_remaining == INITIAL_LOTS:
            _, partial_pts, _ = INDEX_OI_RISK.get(index_code, DEFAULT_OI_RISK)
            if favorable >= partial_pts:
                self._book_partial(index_code, state, spot, f"HALF_TARGET_+{int(partial_pts)}", now)
            pos = state.position
            if pos is None:
                return

        # Full target on remaining lot(s).
        if pos.direction == "LONG":
            if spot >= pos.target_price:
                self._exit_position(index_code, state, spot, "TARGET", now)
        elif spot <= pos.target_price:
            self._exit_position(index_code, state, spot, "TARGET", now)

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
            cache_manager.TRADE_LOG_KEY_TEMPLATE.format(day=now.date().isoformat()),
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
            performance_store.ingest_strategy1_trade_log(
                today,
                self.trade_log,
                paper_trading=PAPER_TRADING,
            )
            day_summary = performance_store.build_day_summary(today)
            payload["completed_trades_summary"] = {
                "trade_count": day_summary["trade_count"],
                "wins": day_summary["wins"],
                "losses": day_summary["losses"],
                "win_pct": day_summary["win_pct"],
                "pnl_points_total": day_summary["pnl_points_total"],
                "by_strategy": day_summary["by_strategy"],
                "by_index": day_summary["by_index"],
                "by_strategy_and_index": day_summary["by_strategy_and_index"],
            }
            out_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
            logger.info(
                "Performance review archived: %s (all strategies %+.2f pts, %d trades)",
                out_path.resolve(),
                day_summary["pnl_points_total"],
                day_summary["trade_count"],
            )
            telegram_notifier.notify_system_event(
                "15:30 DATA ARCHIVAL",
                performance_store.format_day_summary_telegram(
                    out_path.resolve(),
                    day=today,
                    s1_pnl_by_index=dict(self.realized_pnl_points),
                    s1_event_count=len(self.trade_log),
                ),
            )
        except Exception as exc:
            logger.exception("Performance archival failed: %s", exc)

    def _roll_trade_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        for state in self.states.values():
            if state.trade_day != today:
                if state.position is not None:
                    logger.warning(
                        "[%s] open position at day roll — forcing flat (intraday only)",
                        state.config.code,
                    )
                    state.position = None
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
                "pcr": round(state.pcr, 2) if state.pcr is not None else None,
                "max_ce_writing_strike": state.max_ce_writing_strike,
                "max_pe_writing_strike": state.max_pe_writing_strike,
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
                "recent_trades": self._recent_trades_for_index(cfg.code),
                "updated_at": now.isoformat(),
            },
            ttl_seconds=120,
        )

    def _recent_trades_for_index(self, index_code: str) -> list[str]:
        lines: list[str] = []
        for event in self.trade_log:
            if not isinstance(event, dict) or event.get("index") != index_code:
                continue
            kind = str(event.get("event") or "")
            at = str(event.get("at") or "")[:19].replace("T", " ")
            direction = event.get("direction", "—")
            if kind == "ENTRY":
                opt = event.get("option", "")
                entry = event.get("entry_spot")
                lines.append(f"{at} ENTRY {direction} @ {entry} via {opt}")
            elif kind == "PARTIAL_BOOK":
                lines.append(f"{at} PARTIAL {direction} @ {event.get('spot')} ({event.get('reason', '')})")
            elif kind == "EXIT":
                lines.append(
                    f"{at} EXIT {direction} @ {event.get('exit_spot')} "
                    f"({event.get('reason', '')}, {float(event.get('points') or 0):+.2f} pts)"
                )
        return lines[-8:]

    def _publish_global_state(self, now: datetime) -> None:
        positions = {
            code: s.position.as_dict() for code, s in self.states.items() if s.position is not None
        }
        cache_manager.set_json(cache_manager.POSITIONS_KEY, positions)
        cache_manager.set_json(
            cache_manager.ENGINE_HEARTBEAT_KEY,
            {
                "at": now.isoformat(),
                "paper_trading": PAPER_TRADING,
                "entry_open_ist": ENTRY_OPEN_TIME.strftime("%H:%M"),
                "no_entry_after_ist": NO_ENTRY_AFTER.strftime("%H:%M"),
                "square_off_ist": SQUARE_OFF_TIME.strftime("%H:%M"),
                "session_end_ist": ARCHIVE_TIME.strftime("%H:%M"),
            },
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
    results: dict[str, str] = {"status": "kill switch engaged"}

    if PAPER_TRADING or MOCK_MODE:
        return {**results, "note": "paper/mock — no broker orders sent"}

    client = build_upstox_client()
    client.refresh_access_token_from_disk()
    square_offs = client.square_off_all_open_positions(bypass_profit_guard=True)
    if not square_offs:
        positions = cache_manager.get_json(cache_manager.POSITIONS_KEY) or {}
        if isinstance(positions, dict):
            for code, pos in positions.items():
                try:
                    instrument = str(pos.get("instrument_key") or "")
                    if not instrument:
                        results[code] = "paper position flagged for engine square-off"
                        continue
                    ok = client.place_market_order(
                        instrument, int(pos.get("quantity", 0)), "SELL", bypass_profit_guard=True
                    )
                    results[code] = "square-off order sent" if ok else "ORDER FAILED - check broker"
                except Exception as exc:
                    logger.exception("Emergency square-off failed for %s: %s", code, exc)
                    results[code] = "ERROR - see engine logs"
        else:
            results["note"] = "no open Upstox positions found"
    else:
        for row in square_offs:
            sym = str(row.get("symbol") or row.get("instrument") or "?")
            results[sym] = str(row.get("status") or "?")

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
