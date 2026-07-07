"""AK07 Breakout System — Strategy Type 3.

Daily Green / Mid / Red locked at 9:15 session open + instrument band half-width
(Pine v6: Nifty 0.211%, BankNifty 0.125%, Sensex 0.14% of price).

Entry: each 5m **body close** (close price, not wick high/low) through Green/Red
after levels are known from 9:15. First session bar (9:20 close) uses close vs level;
later bars require prior close on the inside (body-close cross, not wick poke).

**Trading disabled by default** (BREAKOUT_ENTRIES_ENABLED=0) after 3-year backtest
showed no edge. Engine still runs to publish BLR levels + day_review for S2 SMC+CRT.

Run: python -u src/server/src/app/services/breakout_engine.py
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager, telegram_notifier
from app.services import performance_store
from app.services.backtest_data import parse_candle_ts
from app.services.engine_intraday import blr_day_review_allows_direction
from app.services.breakout_order_fanout import (
    legs_summary,
    list_live_s3_traders,
    place_s3_entries,
    place_s3_exits,
    position_legs,
)
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    IndexConfig,
    MOCK_MODE,
    PAPER_TRADING,
    UpstoxClient,
    build_upstox_client,
    parse_v3_intraday_candles,
)

logger = logging.getLogger("ak07.breakout_engine")

IST: Final = ZoneInfo("Asia/Kolkata")
CANDLE_5M: Final[int] = 5
POLL_SECONDS: Final[float] = float(os.environ.get("BREAKOUT_POLL_SECONDS", "15"))
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("BREAKOUT_MAX_TRADES_PER_DAY", "3"))
LOTS_PER_TRADE: Final[int] = 1
SL_BUFFER: Final[float] = float(os.environ.get("BREAKOUT_SL_BUFFER_PTS", "2.0"))
# Minimum directional body ratio (body / candle_range). Filters wick-driven false breakouts.
BREAKOUT_MIN_BODY_RATIO: Final[float] = float(os.environ.get("BREAKOUT_MIN_BODY_RATIO", "0"))
DAY_REVIEW_ENABLED: Final[bool] = os.environ.get("BREAKOUT_DAY_REVIEW_ENABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
BREAKOUT_TP1_PTS: Final[dict[str, float]] = {
    "NIFTY": float(os.environ.get("BREAKOUT_TP1_PTS_NIFTY", "80")),
    "BANKNIFTY": float(os.environ.get("BREAKOUT_TP1_PTS_BANKNIFTY", "80")),
    "SENSEX": float(os.environ.get("BREAKOUT_TP1_PTS_SENSEX", "200")),
}
SENSEX_COST_SL_PTS: Final[float] = float(os.environ.get("BREAKOUT_SENSEX_COST_SL_PTS", "50"))
# Sizing: band (production default) or fixed_sl_tp (30 SL / 60 TP trial — matches Pine v8).
SIZING_MODE: Final[str] = os.environ.get("BREAKOUT_SIZING_MODE", "fixed_sl_tp").strip().lower()
FIXED_SL_PTS: Final[float] = float(os.environ.get("BREAKOUT_FIXED_SL_PTS", "30"))
FIXED_TP_PTS: Final[float] = float(os.environ.get("BREAKOUT_FIXED_TP_PTS", "60"))

# Pine v6 band half-width (% of 9:15 session open / Mid)
BAND_HALF_PCT: Final[dict[str, float]] = {
    "NIFTY": float(os.environ.get("BREAKOUT_BAND_PCT_NIFTY", "0.211")),
    "BANKNIFTY": float(os.environ.get("BREAKOUT_BAND_PCT_BANKNIFTY", "0.125")),
    "SENSEX": float(os.environ.get("BREAKOUT_BAND_PCT_SENSEX", "0.14")),
}
GAP_EXTRA_PCT: Final[float] = float(os.environ.get("BREAKOUT_GAP_EXTRA_PCT", "0.0"))
FLAT_GAP_PCT: Final[float] = 0.10
# Pine BLR line tweaks (index points). RED=-3 lowers red line; GREEN=+3 raises green.
GREEN_OFFSET: Final[float] = float(os.environ.get("BREAKOUT_GREEN_OFFSET", "0"))
RED_OFFSET: Final[float] = float(os.environ.get("BREAKOUT_RED_OFFSET", "0"))
# Wait for NSE day OHLC / 5m candle open; do not lock BLR on live LTP before 9:15 bar exists.
ALLOW_PROVISIONAL_LTP: Final[bool] = os.environ.get("BREAKOUT_ALLOW_PROVISIONAL_LTP", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)


def session_open_offset_pts(index_code: str) -> float:
    """Shift 9:15 open to match TradingView when broker feed differs (e.g. NIFTY ~+7)."""
    specific = os.environ.get(f"BREAKOUT_SESSION_OPEN_OFFSET_{index_code}", "").strip()
    if specific:
        return float(specific)
    return float(os.environ.get("BREAKOUT_SESSION_OPEN_OFFSET_PTS", "0") or "0")


def _parse_ist_time(env_key: str, default_hour: int, default_minute: int) -> dtime:
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        parts = raw.replace(".", ":").split(":")
        if len(parts) >= 2:
            try:
                return dtime(int(parts[0]), int(parts[1]))
            except ValueError:
                pass
    return dtime(default_hour, default_minute)


SESSION_START: Final[dtime] = _parse_ist_time("BREAKOUT_SESSION_START_IST", 9, 15)
ENTRY_START: Final[dtime] = _parse_ist_time("BREAKOUT_ENTRY_START_IST", 9, 20)
NO_ENTRY_AFTER: Final[dtime] = _parse_ist_time("BREAKOUT_NO_ENTRY_AFTER_IST", 13, 0)


def _parse_entries_indices() -> frozenset[str]:
    raw = os.environ.get("BREAKOUT_ENTRIES_INDICES", "NIFTY").strip()
    if not raw or raw.lower() in ("all", "*"):
        return frozenset(INDEX_CONFIGS.keys())
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


ENTRIES_INDICES: Final[frozenset[str]] = _parse_entries_indices()
SQUARE_OFF_TIME: Final[dtime] = _parse_ist_time("BREAKOUT_SQUARE_OFF_IST", 14, 55)
SESSION_END: Final[dtime] = _parse_ist_time("BREAKOUT_SESSION_END_IST", 15, 30)

# S3 disabled for trading after 3-year backtest showed no edge (-1,663 pts).
# Engine still runs to publish BLR levels + day_review for S2 SMC+CRT.
# Set BREAKOUT_ENTRIES_ENABLED=1 to re-enable live S3 entries.
ENTRIES_ENABLED: Final[bool] = os.environ.get("BREAKOUT_ENTRIES_ENABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


@dataclass(frozen=True)
class BLRLevels:
    mid: float
    green: float
    red: float
    gap_regime: str
    session_open: float
    band_half: float
    band_half_pct: float
    prev_open: float
    prev_high: float
    prev_low: float
    prev_close: float


@dataclass
class BreakoutPosition:
    direction: str
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    lot_size: int
    instrument_key: str
    contract_label: str
    opened_at: str
    entry_reason: str
    order_legs: list[dict[str, Any]] = field(default_factory=list)
    option_strike: int = 0
    option_type: str = ""

    @property
    def quantity(self) -> int:
        return self.lot_size * LOTS_PER_TRADE

    @property
    def display_contract(self) -> str:
        if self.contract_label:
            return self.contract_label
        if self.option_strike and self.option_type:
            return f"{self.option_strike}{self.option_type}"
        return ""


@dataclass
class IndexBreakoutState:
    config: IndexConfig
    spot: float | None = None
    mid: float | None = None
    green: float | None = None
    red: float | None = None
    gap_regime: str = ""
    band_half: float | None = None
    band_half_pct: float | None = None
    session_open: float | None = None
    broker_session_open: float | None = None
    session_open_tv_offset: float = 0.0
    session_open_source: str = ""
    prev_close: float | None = None
    levels_ready: bool = False
    day_review: str = "PENDING"
    first_candle_close: float | None = None
    trades_today: int = 0
    trade_day: str = ""
    position: BreakoutPosition | None = None
    last_candle_ts: str = ""
    setup_label: str = "Waiting for session"
    signal_log: list[str] = field(default_factory=list)


def _position_to_dict(pos: BreakoutPosition) -> dict[str, Any]:
    return {
        "direction": pos.direction,
        "entry_price": pos.entry_price,
        "sl_price": pos.sl_price,
        "tp1_price": pos.tp1_price,
        "tp2_price": pos.tp2_price,
        "lot_size": pos.lot_size,
        "instrument_key": pos.instrument_key,
        "contract_label": pos.contract_label,
        "order_legs": pos.order_legs,
        "option_strike": pos.option_strike,
        "option_type": pos.option_type,
        "opened_at": pos.opened_at,
        "entry_reason": pos.entry_reason,
    }


def _position_from_dict(raw: dict[str, Any]) -> BreakoutPosition | None:
    try:
        contract_label = str(raw.get("contract_label") or "")
        option_strike = int(raw.get("option_strike") or 0)
        option_type = str(raw.get("option_type") or "")
        if not contract_label and option_strike and option_type:
            contract_label = f"{option_strike}{option_type}"
        legs_raw = raw.get("order_legs")
        order_legs = legs_raw if isinstance(legs_raw, list) else []
        return BreakoutPosition(
            direction=str(raw["direction"]),
            entry_price=float(raw["entry_price"]),
            sl_price=float(raw["sl_price"]),
            tp1_price=float(raw["tp1_price"]),
            tp2_price=float(raw["tp2_price"]),
            lot_size=int(raw["lot_size"]),
            instrument_key=str(raw.get("instrument_key") or ""),
            contract_label=contract_label,
            opened_at=str(raw.get("opened_at") or ""),
            entry_reason=str(raw.get("entry_reason") or ""),
            order_legs=[leg for leg in order_legs if isinstance(leg, dict)],
            option_strike=option_strike,
            option_type=option_type,
        )
    except (KeyError, TypeError, ValueError):
        return None


def compute_blr_levels(
    prev_open: float,
    prev_high: float,
    prev_low: float,
    prev_close: float,
    session_open: float,
    index_code: str,
) -> BLRLevels:
    """Pine v6 BLR: Mid = session open; Green/Red = Mid ± band half-width (% of price)."""
    prev_range = prev_high - prev_low
    safe_range = max(prev_range, 0.05)

    gap = session_open - prev_close
    gap_abs = abs(gap)
    gap_pct = gap_abs / safe_range

    is_gap_up = gap > 0
    is_gap_dn = gap < 0
    is_flat = gap_pct <= FLAT_GAP_PCT

    if is_flat:
        gap_regime = "FLAT"
    elif is_gap_up:
        gap_regime = "GAP_UP"
    else:
        gap_regime = "GAP_DN"

    base = session_open
    active_pct = BAND_HALF_PCT.get(index_code, 0.211)
    half_width = base * active_pct / 100.0
    gap_addon = (
        base * GAP_EXTRA_PCT / 100.0
        if GAP_EXTRA_PCT and (is_gap_up or is_gap_dn)
        else 0.0
    )
    band_half = half_width + gap_addon
    green = base + band_half + GREEN_OFFSET
    red = base - band_half + RED_OFFSET

    return BLRLevels(
        mid=base,
        green=green,
        red=red,
        gap_regime=gap_regime,
        session_open=session_open,
        band_half=band_half,
        band_half_pct=active_pct,
        prev_open=prev_open,
        prev_high=prev_high,
        prev_low=prev_low,
        prev_close=prev_close,
    )


def day_review_from_first_close(first_close: float, mid: float) -> str:
    """First 5m close vs central pivot — which side to review today."""
    if first_close > mid:
        return "LONG"
    if first_close < mid:
        return "SHORT"
    return "NEUTRAL"


def is_first_session_bar(candle_ts: datetime, prev_candle_ts: datetime) -> bool:
    """True when prev bar is pre-session (yesterday or before 9:15) — first 5m close at ~9:20."""
    return prev_candle_ts.date() < candle_ts.date() or prev_candle_ts.time() < SESSION_START


def detect_breakout_signal(
    prev_close: float,
    close: float,
    green: float,
    red: float,
    mid: float,
    day_review: str,
    *,
    first_session_bar: bool = False,
    candle_open: float | None = None,
    candle_high: float | None = None,
    candle_low: float | None = None,
    min_body_ratio: float = 0.0,
    use_day_review: bool | None = None,
) -> tuple[str | None, str]:
    """Return (direction, reason) for a closed 5m body-close breakout.

    Uses **close** only (not wick high/low). After 9:15 BLR lock:
    - First session 5m bar: close > Green or close < Red (+ mid side filter).
    - Later bars: same, but prior bar close must have been on the inside of the level.
    """
    if use_day_review is None:
        use_day_review = DAY_REVIEW_ENABLED

    long_body_close = close > green and close > mid
    short_body_close = close < red and close < mid

    if first_session_bar:
        long_breakout = long_body_close
        short_breakout = short_body_close
    else:
        long_breakout = long_body_close and prev_close <= green
        short_breakout = short_body_close and prev_close >= red

    if min_body_ratio > 0 and candle_open is not None and candle_high is not None and candle_low is not None:
        rng = candle_high - candle_low
        if rng > 0:
            if long_breakout:
                bull_body = (close - candle_open) / rng
                if bull_body < min_body_ratio:
                    return None, f"long wick-breakout filtered (body {bull_body:.2f} < {min_body_ratio:.2f})"
            if short_breakout:
                bear_body = (candle_open - close) / rng
                if bear_body < min_body_ratio:
                    return None, f"short wick-breakout filtered (body {bear_body:.2f} < {min_body_ratio:.2f})"

    if long_breakout:
        if use_day_review and not blr_day_review_allows_direction(day_review, "LONG"):
            return None, f"long body-close blocked (Review {day_review} day)"
        review_note = f" Review {day_review}" if use_day_review else ""
        return "LONG", f"green body-close ({close:.2f} > {green:.2f}){review_note}"

    if short_breakout:
        if use_day_review and not blr_day_review_allows_direction(day_review, "SHORT"):
            return None, f"short body-close blocked (Review {day_review} day)"
        review_note = f" Review {day_review}" if use_day_review else ""
        return "SHORT", f"red body-close ({close:.2f} < {red:.2f}){review_note}"

    return None, ""


def trade_levels(
    index_code: str,
    direction: str,
    entry: float,
    mid: float,
    green: float,
    red: float,
    gap_regime: str,
) -> tuple[float, float, float]:
    """Spot SL, TP1, TP2 from BREAKOUT_SIZING_MODE.

    fixed_sl_tp — entry ± FIXED_SL_PTS / FIXED_TP_PTS (default 30 / 60, 1:2 R:R).
    band — SL at band_half + buffer, TP at 1.5× / 3× band (legacy production).
    """
    if SIZING_MODE in ("fixed", "fixed_sl_tp", "fixed_sl_and_tp"):
        sl_dist = FIXED_SL_PTS
        tp1_pts = FIXED_TP_PTS
        tp2_pts = FIXED_TP_PTS * 2
    else:
        band_half = green - mid  # same day, same band for every bar
        sl_dist = band_half + SL_BUFFER
        tp1_pts = band_half * 1.5  # 1.5:1 R:R
        tp2_pts = band_half * 3.0  # 3:1 R:R
    if direction == "LONG":
        sl = entry - sl_dist
        tp1 = entry + tp1_pts
        tp2 = entry + tp2_pts
    else:
        sl = entry + sl_dist
        tp1 = entry - tp1_pts
        tp2 = entry - tp2_pts
    return sl, tp1, tp2


def _parse_daily_row(row: Any) -> dict[str, float] | None:
    if isinstance(row, (list, tuple)) and len(row) >= 5:
        try:
            ts = datetime.fromisoformat(str(row[0]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            return {
                "date": ts.date().isoformat(),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            }
        except (ValueError, TypeError, IndexError):
            return None
    if isinstance(row, dict):
        try:
            ts_raw = row.get("timestamp") or row.get("time")
            ts = datetime.fromisoformat(str(ts_raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            return {
                "date": ts.date().isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        except (KeyError, TypeError, ValueError):
            return None
    return None


class BreakoutMarketClient:
    def __init__(self) -> None:
        self._upstox: UpstoxClient | None = None if MOCK_MODE else build_upstox_client()
        self._mock_spots: dict[str, float] = {
            "NIFTY": 23_100.0,
            "BANKNIFTY": 51_200.0,
            "SENSEX": 76_400.0,
        }
        self._mock_levels: dict[str, BLRLevels] = {}
        self._tick = 0

    def refresh_token(self) -> None:
        if self._upstox:
            self._upstox.refresh_access_token_from_disk()

    def get_spot(self, cfg: IndexConfig) -> float | None:
        if MOCK_MODE:
            return self._mock_spot(cfg)
        if self._upstox:
            ltp = self._upstox.get_ltp(cfg.spot_instrument_key)
            if ltp is not None:
                self._mock_spots[cfg.code] = ltp
                return ltp
        return self._mock_spot(cfg)

    def _mock_spot(self, cfg: IndexConfig) -> float:
        base = self._mock_spots.get(cfg.code, 23_100.0)
        drift = base * random.uniform(-0.0005, 0.0005)
        value = round(base + drift, 2)
        self._mock_spots[cfg.code] = value
        return value

    def _encoded_instrument_key(self, instrument_key: str) -> str:
        return quote(instrument_key, safe="")

    def get_5m_candles(self, cfg: IndexConfig) -> list[dict[str, float]] | None:
        if MOCK_MODE:
            return self._mock_candles(cfg)
        if not self._upstox:
            return []
        v3_base = self._upstox.base_url.replace("/v2", "/v3")
        key = self._encoded_instrument_key(cfg.spot_instrument_key)
        data = self._upstox._get(  # noqa: SLF001
            f"{v3_base}/historical-candle/intraday/{key}/minutes/{CANDLE_5M}"
        )
        return parse_v3_intraday_candles(data, datetime.now(IST))

    def get_session_day_open(self, cfg: IndexConfig) -> float | None:
        if MOCK_MODE:
            return self._mock_spots.get(cfg.code)
        if not self._upstox:
            return None
        return self._upstox.get_index_day_open(cfg.spot_instrument_key)

    def get_previous_day_ohlc(self, cfg: IndexConfig) -> dict[str, float] | None:
        if MOCK_MODE:
            spot = self._mock_spots.get(cfg.code, 23_100.0)
            width = spot * 0.012
            return {
                "open": spot - width * 0.3,
                "high": spot + width * 0.4,
                "low": spot - width * 0.5,
                "close": spot - width * 0.1,
            }
        if not self._upstox:
            return None

        today = datetime.now(IST).date()
        to_date = today - timedelta(days=1)
        from_date = today - timedelta(days=14)
        key = self._encoded_instrument_key(cfg.spot_instrument_key)
        v3_base = self._upstox.base_url.replace("/v2", "/v3")
        url = f"{v3_base}/historical-candle/{key}/days/1/{to_date.isoformat()}/{from_date.isoformat()}"
        data = self._upstox._get(url)  # noqa: SLF001
        if not isinstance(data, dict):
            # V2 fallback (same candle row format)
            v2_url = (
                f"{self._upstox.base_url}/historical-candle/{key}/day/"
                f"{to_date.isoformat()}/{from_date.isoformat()}"
            )
            data = self._upstox._get(v2_url)  # noqa: SLF001
        if not isinstance(data, dict):
            logger.warning("[%s] previous-day OHLC fetch failed", cfg.code)
            return None

        rows = data.get("candles") or []
        if not isinstance(rows, list) or not rows:
            logger.warning("[%s] previous-day OHLC returned no candles", cfg.code)
            return None

        best: dict[str, float] | None = None
        best_day: date | None = None
        for row in rows:
            parsed = _parse_daily_row(row)
            if not parsed:
                continue
            row_day = date.fromisoformat(parsed["date"])
            if row_day >= today:
                continue
            if best_day is None or row_day > best_day:
                best_day = row_day
                best = parsed
        if best:
            logger.info("[%s] previous-day OHLC from %s", cfg.code, best_day)
            return best
        logger.warning("[%s] no prior session OHLC before %s in %d rows", cfg.code, today, len(rows))
        return None

    def _mock_candles(self, cfg: IndexConfig) -> list[dict[str, float]]:
        now = datetime.now(IST)
        spot = self._mock_spots.get(cfg.code, 23_100.0)
        if cfg.code not in self._mock_levels:
            prev = {
                "open": spot - 120,
                "high": spot + 80,
                "low": spot - 180,
                "close": spot - 40,
            }
            levels = compute_blr_levels(
                prev["open"], prev["high"], prev["low"], prev["close"],
                session_open=spot,
                index_code=cfg.code,
            )
            self._mock_levels[cfg.code] = levels

        levels = self._mock_levels[cfg.code]
        self._tick += 1
        ts = datetime.combine(now.date(), SESSION_START, tzinfo=IST)
        bar_open = levels.mid
        close = levels.mid + (levels.green - levels.mid) * 0.15
        if self._tick >= 4:
            close = levels.green + 5
        elif self._tick >= 2:
            close = levels.mid + (levels.green - levels.mid) * 0.55
        return [
            {
                "timestamp": ts.isoformat(),
                "open": bar_open,
                "high": max(bar_open, close) + 12,
                "low": min(bar_open, close) - 15,
                "close": close,
                "volume": 90_000,
            }
        ]

    def resolve_future(self, cfg: IndexConfig) -> dict[str, Any] | None:
        if self._upstox and not MOCK_MODE:
            contract = self._upstox.get_index_future_contract(cfg.code)
            if contract:
                return contract
        return {
            "instrument_key": "",
            "trading_symbol": cfg.code,
            "expiry": "",
            "contract_label": f"{cfg.code} FUT",
        }


class BreakoutEngine:
    def __init__(self) -> None:
        self.client = BreakoutMarketClient()
        self.states = {code: IndexBreakoutState(config=cfg) for code, cfg in INDEX_CONFIGS.items()}
        now = datetime.now(IST)
        for state in self.states.values():
            state.trade_day = now.date().isoformat()
            self._restore_frozen_levels(state, now.date().isoformat())
            self._restore_session_state(state, now.date().isoformat())
        traders = list_live_s3_traders()
        logger.info(
            "Breakout engine started (paper=%s mock=%s entries=%s indices=%s sizing=%s sl=%.0f tp=%.0f max_trades=%d no_entry_after=%s lot=%d live_traders=%s)",
            PAPER_TRADING,
            MOCK_MODE,
            ENTRIES_ENABLED,
            ",".join(sorted(ENTRIES_INDICES)),
            SIZING_MODE,
            FIXED_SL_PTS,
            FIXED_TP_PTS,
            MAX_TRADES_PER_DAY,
            NO_ENTRY_AFTER.strftime("%H:%M"),
            LOTS_PER_TRADE,
            ",".join(f"{t.username}@{t.broker}" for t in traders) or "none",
        )
        if not ENTRIES_ENABLED:
            logger.warning(
                "S3 BLR Breakout entries DISABLED — publishing BLR/day-review only (S2 filter). "
                "Set BREAKOUT_ENTRIES_ENABLED=1 to trade."
            )

    def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                logger.exception("Breakout tick failed: %s", exc)
            time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))

    def tick(self) -> None:
        now = datetime.now(IST)
        self.client.refresh_token()
        self._roll_trade_day(now)

        if now.time() >= SESSION_END:
            self._square_off_all("SESSION_END", now)
            for state in self.states.values():
                spot = self.client.get_spot(state.config)
                if spot is not None:
                    state.spot = spot
                self._restore_frozen_levels(state, now.date().isoformat())
                if state.levels_ready:
                    state.setup_label = (
                        f"Session closed — BLR frozen G {state.green:.2f} / "
                        f"M {state.mid:.2f} / R {state.red:.2f} · review {state.day_review}"
                    )
            self._publish_all(now, entries_blocked=True, block_reason="session closed")
            return

        kill = self._kill_switch_engaged()
        entries_blocked = kill or now.time() < SESSION_START
        block_reason = ""
        if not ENTRIES_ENABLED:
            entries_blocked = True
            block_reason = "S3 disabled (backtest: no edge) — BLR/day-review publish only"
        if kill:
            self._square_off_all("KILL_SWITCH", now)

        if now.time() >= SQUARE_OFF_TIME:
            self._square_off_all("SESSION_SQUARE_OFF", now)
            entries_blocked = True

        for state in self.states.values():
            index_block = block_reason
            index_blocked = entries_blocked
            if state.config.code not in ENTRIES_INDICES:
                index_blocked = True
                if not block_reason:
                    index_block = (
                        f"Entries off for {state.config.code} "
                        f"(live indices: {', '.join(sorted(ENTRIES_INDICES))})"
                    )
            self._process_index(state, now, index_blocked, index_block)
        self._publish_heartbeat(now)

    def _roll_trade_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        for state in self.states.values():
            if state.trade_day != today:
                if state.position is not None:
                    logger.warning(
                        "[%s] breakout open position at day roll — forcing flat (intraday only)",
                        state.config.code,
                    )
                state.trade_day = today
                state.trades_today = 0
                state.position = None
                state.levels_ready = False
                state.mid = state.green = state.red = None
                state.session_open = None
                state.broker_session_open = None
                state.session_open_tv_offset = 0.0
                state.session_open_source = ""
                state.prev_close = None
                state.band_half = state.band_half_pct = None
                state.gap_regime = ""
                state.day_review = "PENDING"
                state.first_candle_close = None
                state.last_candle_ts = ""
                state.setup_label = "New session — building BLR levels"

    def _process_index(
        self,
        state: IndexBreakoutState,
        now: datetime,
        entries_blocked: bool,
        block_reason: str = "",
    ) -> None:
        cfg = state.config
        if now.time() < SESSION_START:
            state.setup_label = f"Pre-market — session from {SESSION_START.strftime('%H:%M')} IST"
            self._publish_state(state, now, entries_blocked=True)
            return

        spot = self.client.get_spot(cfg)
        if spot is not None:
            state.spot = spot

        candles = self.client.get_5m_candles(cfg) or []
        self._refresh_levels(state, candles, now, spot)

        if state.position:
            self._manage_position(state, now, candles)
        elif (
            not entries_blocked
            and state.levels_ready
            and state.day_review not in ("", "PENDING")
            and now.time() >= ENTRY_START
            and state.trades_today < MAX_TRADES_PER_DAY
            and candles
        ):
            self._seek_entry(state, candles, now)

        self._publish_state(state, now, entries_blocked, block_reason)

    _SESSION_OPEN_RANK: Final[dict[str, int]] = {
        "ltp_provisional": 0,
        "candle": 1,
        "day_ohlc": 2,
    }

    def _session_open_source_rank(self, source: str) -> int:
        return self._SESSION_OPEN_RANK.get(source, -1)

    def _best_session_open(
        self,
        state: IndexBreakoutState,
        candles: list[dict[str, float]],
        now: datetime,
        spot: float | None,
    ) -> tuple[float | None, str]:
        """Best available 9:15 open: NSE day OHLC (TV parity) > 5m candle > optional LTP."""
        if now.time() < SESSION_START:
            return None, ""

        day_open = self.client.get_session_day_open(state.config)
        candle_open: float | None = None
        first = self._first_session_candle(candles, now.date())
        if first is not None:
            candle_open = float(first["open"])

        if day_open is not None and candle_open is not None and abs(day_open - candle_open) >= 0.5:
            logger.info(
                "[%s] session open day_ohlc=%.2f vs 5m candle=%.2f — using day_ohlc",
                state.config.code,
                day_open,
                candle_open,
            )

        if day_open is not None:
            return day_open, "day_ohlc"
        if candle_open is not None:
            return candle_open, "candle"
        if ALLOW_PROVISIONAL_LTP and spot is not None:
            return spot, "ltp_provisional"
        return None, ""

    def _should_upgrade_session_open(
        self,
        cur_source: str,
        cur_open: float,
        new_open: float,
        new_source: str,
    ) -> bool:
        if self._session_open_source_rank(new_source) <= self._session_open_source_rank(cur_source):
            return False
        return abs(new_open - cur_open) >= 0.01

    def _resolve_session_open(
        self,
        state: IndexBreakoutState,
        candles: list[dict[str, float]],
        now: datetime,
        spot: float | None,
    ) -> tuple[float | None, str]:
        if state.broker_session_open is not None:
            return state.broker_session_open, state.session_open_source or "frozen"
        return self._best_session_open(state, candles, now, spot)

    def _first_session_candle(self, candles: list[dict[str, float]], day: date) -> dict[str, float] | None:
        for candle in candles:
            ts = datetime.fromisoformat(candle["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.date() == day and ts.time() == SESSION_START:
                return candle
        for candle in candles:
            ts = datetime.fromisoformat(candle["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.date() == day and ts.time() >= SESSION_START:
                return candle
        return None

    def _lock_blr_levels(
        self,
        state: IndexBreakoutState,
        opening_915: float,
        open_source: str,
        prev: dict[str, float],
    ) -> None:
        broker_open = opening_915
        tv_offset = session_open_offset_pts(state.config.code)
        effective_open = broker_open + tv_offset
        levels = compute_blr_levels(
            prev["open"],
            prev["high"],
            prev["low"],
            prev["close"],
            effective_open,
            state.config.code,
        )
        state.broker_session_open = broker_open
        state.session_open_tv_offset = tv_offset
        state.session_open = effective_open
        state.session_open_source = open_source
        state.prev_close = prev["close"]
        state.mid = levels.mid
        state.green = levels.green
        state.red = levels.red
        state.gap_regime = levels.gap_regime
        state.band_half = levels.band_half
        state.band_half_pct = levels.band_half_pct
        state.levels_ready = True
        src_notes = {
            "candle": "9:15 candle open",
            "day_ohlc": "NSE day open",
            "ltp_provisional": "provisional LTP",
        }
        src_note = src_notes.get(open_source, open_source or "session open")
        open_note = f"{src_note} {broker_open:.2f}"
        if tv_offset:
            open_note = f"{open_note} + TV {tv_offset:+.2f} = {effective_open:.2f}"
        base_label = (
            f"BLR locked — G {levels.green:.2f} / M {levels.mid:.2f} / R {levels.red:.2f} "
            f"({levels.gap_regime} · {levels.band_half_pct:.3f}% half · {open_note})"
        )
        state.setup_label = base_label
        state.signal_log.append(base_label)
        self._save_frozen_levels(state)
        logger.info(
            "[%s] BLR locked — G %.2f / M %.2f / R %.2f (%s · %.3f%% half · %s · prevC %.2f)",
            state.config.code,
            levels.green,
            levels.mid,
            levels.red,
            levels.gap_regime,
            levels.band_half_pct,
            open_note,
            prev["close"],
        )

    def _refresh_levels(
        self,
        state: IndexBreakoutState,
        candles: list[dict[str, float]],
        now: datetime,
        spot: float | None,
    ) -> None:
        if state.levels_ready:
            best_open, best_source = self._best_session_open(state, candles, now, spot)
            cur_broker = state.broker_session_open
            if cur_broker is None and state.session_open is not None:
                cur_broker = state.session_open - state.session_open_tv_offset
            cur_broker = cur_broker or 0.0
            cur_source = state.session_open_source or ""
            tv_offset = session_open_offset_pts(state.config.code)
            offset_changed = abs(tv_offset - state.session_open_tv_offset) >= 0.01
            if best_open is not None and self._should_upgrade_session_open(
                cur_source, cur_broker, best_open, best_source
            ):
                logger.info(
                    "[%s] Re-locking BLR from %s %.2f -> %s %.2f",
                    state.config.code,
                    cur_source or "unknown",
                    cur_broker,
                    best_source,
                    best_open,
                )
                state.levels_ready = False
                state.session_open = None
                state.broker_session_open = None
                state.session_open_tv_offset = 0.0
                state.session_open_source = ""
                state.mid = state.green = state.red = None
            elif offset_changed:
                logger.info(
                    "[%s] Re-locking BLR — TV offset %.2f -> %.2f",
                    state.config.code,
                    state.session_open_tv_offset,
                    tv_offset,
                )
                state.levels_ready = False
                state.session_open = None
                state.broker_session_open = None
                state.session_open_tv_offset = 0.0
                state.session_open_source = ""
                state.mid = state.green = state.red = None
            else:
                first = self._first_session_candle(candles, now.date())
                if first and state.day_review == "PENDING":
                    close = float(first["close"])
                    state.first_candle_close = close
                    if state.mid is not None:
                        state.day_review = day_review_from_first_close(close, state.mid)
                        state.setup_label = (
                            f"Review {state.day_review} side "
                            f"(1st 5m close {close:.2f} vs mid {state.mid:.2f})"
                        )
                        msg = (
                            f"{state.config.display} day review={state.day_review} "
                            f"(1st 5m {close:.2f} vs mid {state.mid:.2f})"
                        )
                        state.signal_log.append(msg)
                        logger.info(msg)
                return

        opening_915, open_source = self._resolve_session_open(state, candles, now, spot)
        if opening_915 is None:
            state.setup_label = "Waiting for 9:15 open (NSE day OHLC or 5m candle)"
            return

        prev = self.client.get_previous_day_ohlc(state.config)
        if prev is None:
            state.setup_label = "Waiting for previous day OHLC"
            return

        self._lock_blr_levels(state, opening_915, open_source, prev)

        first = self._first_session_candle(candles, now.date())
        if first:
            close = float(first["close"])
            state.first_candle_close = close
            state.day_review = day_review_from_first_close(close, state.mid or opening_915)
            state.setup_label = (
                f"Review {state.day_review} side "
                f"(1st 5m close {close:.2f} vs mid {state.mid:.2f})"
            )
        elif state.day_review == "PENDING":
            state.setup_label = f"{state.setup_label} — awaiting 1st 5m close for day review"

    def _session_key(self, day: str, index_code: str) -> str:
        return cache_manager.BREAKOUT_SESSION_KEY_TEMPLATE.format(day=day, index=index_code)

    def _save_session_state(self, state: IndexBreakoutState) -> None:
        day = state.trade_day or datetime.now(IST).date().isoformat()
        payload: dict[str, Any] = {
            "trades_today": state.trades_today,
            "last_candle_ts": state.last_candle_ts,
            "signal_log": state.signal_log[-20:],
            "position": _position_to_dict(state.position) if state.position else None,
        }
        cache_manager.set_json(self._session_key(day, state.config.code), payload, ttl_seconds=86_400 * 2)

    def _restore_session_state(self, state: IndexBreakoutState, day: str) -> bool:
        raw = cache_manager.get_json(self._session_key(day, state.config.code))
        if not isinstance(raw, dict):
            return False
        state.trades_today = int(raw.get("trades_today") or 0)
        state.last_candle_ts = str(raw.get("last_candle_ts") or "")
        logs = raw.get("signal_log")
        if isinstance(logs, list) and logs:
            state.signal_log = [str(line) for line in logs[-20:]]
        pos_raw = raw.get("position")
        if isinstance(pos_raw, dict):
            pos = _position_from_dict(pos_raw)
            if pos is not None:
                state.position = pos
                state.setup_label = (
                    f"{pos.direction} open — restored "
                    f"{pos.display_contract} @ spot {pos.entry_price:.2f}"
                )
                logger.info(
                    "[%s] restored breakout position %s %s @ %.2f (SL %.2f TP1 %.2f)",
                    state.config.code,
                    pos.direction,
                    pos.display_contract,
                    pos.entry_price,
                    pos.sl_price,
                    pos.tp1_price,
                )
        if state.trades_today or state.position:
            logger.info(
                "[%s] session restored — trades_today=%d position=%s",
                state.config.code,
                state.trades_today,
                "open" if state.position else "flat",
            )
        return bool(state.trades_today or state.position)

    def _frozen_key(self, day: str, index_code: str) -> str:
        return cache_manager.BREAKOUT_FROZEN_KEY_TEMPLATE.format(day=day, index=index_code)

    def _save_frozen_levels(self, state: IndexBreakoutState) -> None:
        if not state.levels_ready or state.mid is None:
            return
        day = state.trade_day or datetime.now(IST).date().isoformat()
        cache_manager.set_json(
            self._frozen_key(day, state.config.code),
            {
                "mid": state.mid,
                "green": state.green,
                "red": state.red,
                "gap_regime": state.gap_regime,
                "band_half": state.band_half,
                "band_half_pct": state.band_half_pct,
                "session_open": state.session_open,
                "broker_session_open": state.broker_session_open,
                "session_open_tv_offset": state.session_open_tv_offset,
                "session_open_source": state.session_open_source,
                "prev_close": state.prev_close,
                "day_review": state.day_review,
                "first_candle_close": state.first_candle_close,
            },
            ttl_seconds=86_400 * 2,
        )

    def _restore_frozen_levels(self, state: IndexBreakoutState, day: str) -> bool:
        if state.levels_ready and state.mid is not None:
            return True
        frozen = cache_manager.get_json(self._frozen_key(day, state.config.code))
        if not isinstance(frozen, dict) or frozen.get("mid") is None:
            return False
        state.mid = float(frozen["mid"])
        state.green = float(frozen.get("green") or 0)
        state.red = float(frozen.get("red") or 0)
        state.gap_regime = str(frozen.get("gap_regime") or "")
        state.band_half = frozen.get("band_half")
        state.band_half_pct = frozen.get("band_half_pct")
        state.session_open = frozen.get("session_open")
        if frozen.get("broker_session_open") is not None:
            state.broker_session_open = float(frozen["broker_session_open"])
        elif state.session_open is not None:
            state.broker_session_open = float(state.session_open) - float(
                frozen.get("session_open_tv_offset") or 0
            )
        state.session_open_tv_offset = float(frozen.get("session_open_tv_offset") or 0)
        state.session_open_source = str(frozen.get("session_open_source") or "frozen")
        state.prev_close = frozen.get("prev_close")
        state.day_review = str(frozen.get("day_review") or state.day_review)
        if frozen.get("first_candle_close") is not None:
            state.first_candle_close = float(frozen["first_candle_close"])
        state.levels_ready = True
        state.setup_label = (
            f"BLR restored — G {state.green:.2f} / M {state.mid:.2f} / R {state.red:.2f} "
            f"(review {state.day_review})"
        )
        return True

    def _seek_entry(
        self,
        state: IndexBreakoutState,
        candles: list[dict[str, float]],
        now: datetime,
    ) -> None:
        if state.mid is None or state.green is None or state.red is None:
            return

        if DAY_REVIEW_ENABLED and state.day_review in ("", "PENDING"):
            state.setup_label = "Awaiting 9:20 5m close for day review"
            return

        if len(candles) < 2:
            return

        candle = candles[-1]
        if candle["timestamp"] == state.last_candle_ts:
            return

        candle_ts = parse_candle_ts(candle["timestamp"])
        if candle_ts.time() > NO_ENTRY_AFTER:
            state.setup_label = f"Past {NO_ENTRY_AFTER.strftime('%H:%M')} — no new entries (flat {SQUARE_OFF_TIME.strftime('%H:%M')})"
            return

        prev_candle = candles[-2]
        prev_close = float(prev_candle["close"])
        close = float(candle["close"])
        state.last_candle_ts = candle["timestamp"]

        prev_ts = parse_candle_ts(prev_candle["timestamp"])
        first_bar = is_first_session_bar(candle_ts, prev_ts)

        direction, reason = detect_breakout_signal(
            prev_close,
            close,
            state.green,
            state.red,
            state.mid,
            state.day_review,
            first_session_bar=first_bar,
            candle_open=float(candle["open"]),
            candle_high=float(candle["high"]),
            candle_low=float(candle["low"]),
            min_body_ratio=BREAKOUT_MIN_BODY_RATIO,
        )
        if direction is None:
            blocked = reason or f"Watching breakouts (Review {state.day_review} day filter)"
            state.setup_label = blocked
            return

        sl, tp1, tp2 = trade_levels(
            state.config.code,
            direction,
            close,
            state.mid,
            state.green,
            state.red,
            state.gap_regime,
        )
        contract = self.client.resolve_future(state.config)
        if contract is None:
            logger.error("[%s] breakout entry aborted — no futures contract", state.config.code)
            return

        legs = place_s3_entries(
            index_code=state.config.code,
            direction=direction,
            lot_size=state.config.lot_size,
            lots=LOTS_PER_TRADE,
            upstox_market_client=self.client._upstox,
            global_paper=PAPER_TRADING or MOCK_MODE,
        )
        if not legs:
            logger.error("[%s] breakout entry aborted — no broker orders placed", state.config.code)
            return

        contract_label = str(
            legs[0].get("contract_label") or contract.get("contract_label") or f"{state.config.code} FUT"
        )
        primary_key = str(legs[0].get("instrument_key") or contract.get("instrument_key") or "")
        fanout_note = legs_summary(legs)
        state.position = BreakoutPosition(
            direction=direction,
            entry_price=close,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            lot_size=state.config.lot_size,
            instrument_key=primary_key,
            contract_label=contract_label,
            opened_at=now.isoformat(),
            entry_reason=reason,
            order_legs=legs,
        )
        state.trades_today += 1
        msg = (
            f"{state.config.display} BREAKOUT {direction} @ {close:.2f} "
            f"via {contract_label} x{LOTS_PER_TRADE} lot — {reason} "
            f"[{fanout_note}] "
            f"SL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f} (book @ TP1)"
        )
        state.setup_label = f"{direction} entry — {reason}"
        state.signal_log.append(msg)
        logger.info(msg)
        self._save_session_state(state)
        telegram_notifier.notify_trade_execution(
            index_name=f"{state.config.display} Breakout ({contract_label} x{LOTS_PER_TRADE})",
            trade_type=direction,
            entry_price=close,
            target_price=tp1,
            sl_price=sl,
            tp2_price=tp2,
            component_sentiment=state.gap_regime,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
            candles=candles,
        )

    def _position_price_extremes(
        self,
        pos: BreakoutPosition,
        spot: float,
        candles: list[dict[str, float]],
    ) -> tuple[float, float]:
        """Session highs/lows since entry — catches TP/SL wicks LTP polls miss."""
        high = low = spot
        entry_ts: datetime | None = None
        if pos.opened_at:
            try:
                entry_ts = datetime.fromisoformat(pos.opened_at)
                if entry_ts.tzinfo is None:
                    entry_ts = entry_ts.replace(tzinfo=IST)
            except ValueError:
                entry_ts = None
        for candle in candles:
            ts = parse_candle_ts(candle["timestamp"])
            if entry_ts is not None and ts < entry_ts:
                continue
            high = max(high, float(candle["high"]))
            low = min(low, float(candle["low"]))
        return high, low

    def _manage_position(
        self,
        state: IndexBreakoutState,
        now: datetime,
        candles: list[dict[str, float]] | None = None,
    ) -> None:
        pos = state.position
        if pos is None or state.spot is None:
            return

        spot = state.spot
        bar_high, bar_low = self._position_price_extremes(pos, spot, candles or [])
        # Sensex: after +50 pts move SL to entry (cost)
        if state.config.code == "SENSEX":
            fav = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
            if fav >= SENSEX_COST_SL_PTS:
                if pos.direction == "LONG" and pos.sl_price < pos.entry_price:
                    pos.sl_price = pos.entry_price
                elif pos.direction == "SHORT" and pos.sl_price > pos.entry_price:
                    pos.sl_price = pos.entry_price

        exit_reason = ""
        exit_price = spot
        if pos.direction == "LONG":
            if bar_low <= pos.sl_price:
                exit_reason = "SL"
                exit_price = pos.sl_price
            elif bar_high >= pos.tp1_price:
                exit_reason = "TP1 booked"
                exit_price = pos.tp1_price
        else:
            if bar_high >= pos.sl_price:
                exit_reason = "SL"
                exit_price = pos.sl_price
            elif bar_low <= pos.tp1_price:
                exit_reason = "TP1 booked"
                exit_price = pos.tp1_price

        if not exit_reason:
            return

        if not place_s3_exits(position_legs(pos), pos.direction, global_paper=PAPER_TRADING or MOCK_MODE):
            logger.error("[%s] breakout exit order failed (%s)", state.config.code, exit_reason)
            return

        pnl = (
            (exit_price - pos.entry_price)
            if pos.direction == "LONG"
            else (pos.entry_price - exit_price)
        )
        msg = (
            f"{state.config.display} BREAKOUT exit {exit_reason} @ {exit_price:.2f} "
            f"(spot {spot:.2f} bar {bar_low:.2f}-{bar_high:.2f}) ({pnl:+.2f} pts)"
        )
        state.signal_log.append(msg)
        logger.info(msg)
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_BREAKOUT,
            strategy_id="breakout",
            symbol=state.config.code,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl_points=pnl,
            exit_reason=exit_reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
        )
        state.setup_label = f"Flat after {exit_reason}"
        state.position = None
        self._save_session_state(state)
        telegram_notifier.notify_trade_exit(
            index_name=f"{state.config.display} Breakout ({pos.display_contract})",
            trade_type=pos.direction,
            exit_price=exit_price,
            pnl_points=pnl,
            reason=exit_reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _square_off_all(self, reason: str, now: datetime) -> None:
        for state in self.states.values():
            if state.position is not None:
                spot = state.spot if state.spot is not None else state.position.entry_price
                pos = state.position
                place_s3_exits(position_legs(pos), pos.direction, global_paper=PAPER_TRADING or MOCK_MODE)
                pnl = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
                performance_store.record_completed_trade(
                    strategy=performance_store.STRATEGY_BREAKOUT,
                    strategy_id="breakout",
                    symbol=state.config.code,
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    exit_price=spot,
                    pnl_points=pnl,
                    exit_reason=reason,
                    entry_at=pos.opened_at,
                    paper_trading=PAPER_TRADING,
                )
                state.signal_log.append(f"Square-off {reason} @ {spot:.2f}")
                state.position = None
                state.setup_label = f"Flat — {reason}"
                self._save_session_state(state)
                telegram_notifier.notify_trade_exit(
                    index_name=f"{state.config.display} Breakout",
                    trade_type=pos.direction,
                    exit_price=spot,
                    pnl_points=pnl,
                    reason=reason,
                    timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
                )

    def _kill_switch_engaged(self) -> bool:
        flag = cache_manager.get_json(cache_manager.KILL_SWITCH_KEY)
        return bool(flag and flag.get("engaged"))

    def _publish_all(self, now: datetime, entries_blocked: bool, block_reason: str = "") -> None:
        for state in self.states.values():
            self._publish_state(state, now, entries_blocked, block_reason)
        self._publish_heartbeat(now)

    def _publish_state(
        self,
        state: IndexBreakoutState,
        now: datetime,
        entries_blocked: bool,
        block_reason: str = "",
    ) -> None:
        if state.mid is None or state.green is None or state.red is None:
            self._restore_frozen_levels(state, now.date().isoformat())
        pos = state.position
        payload: dict[str, Any] = {
            "index": state.config.code,
            "display": state.config.display,
            "strategy": "Breakout",
            "spot": state.spot,
            "mid": state.mid,
            "green": state.green,
            "red": state.red,
            "gap_regime": state.gap_regime,
            "allowed_long": blr_day_review_allows_direction(state.day_review, "LONG"),
            "allowed_short": blr_day_review_allows_direction(state.day_review, "SHORT"),
            "band_half": state.band_half,
            "band_half_pct": state.band_half_pct,
            "sizing_mode": SIZING_MODE,
            "fixed_sl_pts": FIXED_SL_PTS,
            "fixed_tp_pts": FIXED_TP_PTS,
            "session_open": state.session_open,
            "broker_session_open": state.broker_session_open,
            "session_open_tv_offset": state.session_open_tv_offset,
            "session_open_source": state.session_open_source,
            "prev_close": state.prev_close,
            "levels_ready": state.levels_ready,
            "day_review": state.day_review,
            "first_candle_close": state.first_candle_close,
            "setup_label": state.setup_label,
            "trades_today": state.trades_today,
            "max_trades": MAX_TRADES_PER_DAY,
            "entries_blocked": entries_blocked,
            "entries_enabled": ENTRIES_ENABLED,
            "block_reason": block_reason,
            "paper_trading": PAPER_TRADING,
            "signals": state.signal_log[-10:],
            "session_end_ist": SESSION_END.strftime("%H:%M"),
            "square_off_ist": SQUARE_OFF_TIME.strftime("%H:%M"),
            "no_entry_after_ist": NO_ENTRY_AFTER.strftime("%H:%M"),
            "tp1_points": BREAKOUT_TP1_PTS.get(state.config.code, 50.0),
            "updated_at": now.isoformat(),
        }
        if pos:
            payload["position"] = {
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "sl_price": pos.sl_price,
                "tp1_price": pos.tp1_price,
                "tp2_price": pos.tp2_price,
                "contract_label": pos.contract_label,
                "option_strike": pos.option_strike,
                "option_type": pos.option_type,
                "entry_reason": pos.entry_reason,
                "opened_at": pos.opened_at,
                "order_legs": pos.order_legs,
            }
        key = cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=state.config.code)
        cache_manager.set_json(key, payload, ttl_seconds=86_400)

    def _publish_heartbeat(self, now: datetime) -> None:
        cache_manager.set_json(
            cache_manager.BREAKOUT_HEARTBEAT_KEY,
            {
                "at": now.isoformat(),
                "paper_trading": PAPER_TRADING,
                "mock": MOCK_MODE,
                "session_end_ist": SESSION_END.strftime("%H:%M"),
                "square_off_ist": SQUARE_OFF_TIME.strftime("%H:%M"),
                "no_entry_after_ist": NO_ENTRY_AFTER.strftime("%H:%M"),
                "indices": list(INDEX_CONFIGS.keys()),
                "entries_indices": sorted(ENTRIES_INDICES),
                "entries_enabled": ENTRIES_ENABLED,
            },
            ttl_seconds=60,
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    BreakoutEngine().run()


if __name__ == "__main__":
    main()
