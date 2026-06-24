"""AK07 Breakout System — Strategy Type 3.

Daily Green / Mid / Red from 9:15 session open + instrument band half-width
(Pine v6: Nifty 0.25%, BankNifty 0.125%, Sensex 0.14% of price). Day-review filter
from 9:20 first 5m close vs Mid: Review LONG → longs only, Review SHORT → shorts only.

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
from app.services.engine_intraday import blr_day_review_allows_direction
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    ITM_OFFSET_POINTS,
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
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("BREAKOUT_MAX_TRADES_PER_DAY", "2"))
LOTS_PER_TRADE: Final[int] = 1
SL_BUFFER: Final[float] = float(os.environ.get("BREAKOUT_SL_BUFFER_PTS", "2.0"))
# Minimum directional body ratio (body / candle_range). Filters wick-driven false breakouts.
BREAKOUT_MIN_BODY_RATIO: Final[float] = float(os.environ.get("BREAKOUT_MIN_BODY_RATIO", "0.35"))
BREAKOUT_TP1_PTS: Final[dict[str, float]] = {
    "NIFTY": float(os.environ.get("BREAKOUT_TP1_PTS_NIFTY", "80")),
    "BANKNIFTY": float(os.environ.get("BREAKOUT_TP1_PTS_BANKNIFTY", "80")),
    "SENSEX": float(os.environ.get("BREAKOUT_TP1_PTS_SENSEX", "200")),
}
SENSEX_COST_SL_PTS: Final[float] = float(os.environ.get("BREAKOUT_SENSEX_COST_SL_PTS", "50"))

# Pine v6 band half-width (% of 9:15 session open / Mid)
BAND_HALF_PCT: Final[dict[str, float]] = {
    "NIFTY": float(os.environ.get("BREAKOUT_BAND_PCT_NIFTY", "0.25")),
    "BANKNIFTY": float(os.environ.get("BREAKOUT_BAND_PCT_BANKNIFTY", "0.125")),
    "SENSEX": float(os.environ.get("BREAKOUT_BAND_PCT_SENSEX", "0.14")),
}
GAP_EXTRA_PCT: Final[float] = float(os.environ.get("BREAKOUT_GAP_EXTRA_PCT", "0.0"))
FLAT_GAP_PCT: Final[float] = 0.10
GREEN_OFFSET: Final[float] = 0.0
RED_OFFSET: Final[float] = 0.0


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
    option_strike: int
    option_type: str
    opened_at: str
    entry_reason: str

    @property
    def quantity(self) -> int:
        return self.lot_size * LOTS_PER_TRADE


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
    active_pct = BAND_HALF_PCT.get(index_code, 0.25)
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


def detect_breakout_signal(
    prev_close: float,
    close: float,
    green: float,
    red: float,
    mid: float,
    day_review: str,
    *,
    candle_open: float | None = None,
    candle_high: float | None = None,
    candle_low: float | None = None,
    min_body_ratio: float = 0.0,
) -> tuple[str | None, str]:
    """Return (direction, reason) for a closed 5m breakout (9:20 day-review filter).

    Optional body-ratio confirmation: if min_body_ratio > 0, the candle must have
    a directional body of at least that fraction of its total range. This filters
    wick-driven false breakouts (e.g. big BANKNIFTY opening candles where price
    spikes above the green level but closes back near open).
    """
    long_breakout = prev_close <= green and close > green and close > mid
    short_breakout = prev_close >= red and close < red and close < mid

    # Body-ratio confirmation gate
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

    if long_breakout and blr_day_review_allows_direction(day_review, "LONG"):
        return "LONG", f"green breakout (Review {day_review})"

    if short_breakout and blr_day_review_allows_direction(day_review, "SHORT"):
        return "SHORT", f"red breakdown (Review {day_review})"

    if long_breakout:
        return None, f"long breakout blocked (Review {day_review} day)"
    if short_breakout:
        return None, f"short breakout blocked (Review {day_review} day)"

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
    """Spot SL, TP1, TP2 with entry-anchored fixed-risk sizing.

    SL distance = band_half (green - mid), giving a constant R:R = 1.5:1
    regardless of how extended the breakout candle is.  This prevents the
    'blow-through' problem where a big opening candle creates a 200+ pt loss
    even though the intended SL level was only ~60 pts away.
    """
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

    def resolve_option(self, cfg: IndexConfig, spot: float, direction: str) -> dict[str, Any] | None:
        if self._upstox and not MOCK_MODE:
            contract = self._upstox.get_itm_option_contract(cfg.spot_instrument_key, spot, direction)
            if contract:
                return contract
        desired = spot - ITM_OFFSET_POINTS if direction == "LONG" else spot + ITM_OFFSET_POINTS
        strike = int(round(desired / cfg.strike_step) * cfg.strike_step)
        return {
            "instrument_key": "",
            "strike": strike,
            "option_type": "CE" if direction == "LONG" else "PE",
        }

    def place_entry(self, instrument_key: str, quantity: int) -> bool:
        if PAPER_TRADING or not instrument_key:
            return True
        if self._upstox:
            return self._upstox.place_market_order(instrument_key, quantity, "BUY")
        return False

    def place_exit(self, instrument_key: str, quantity: int) -> bool:
        if PAPER_TRADING or not instrument_key:
            return True
        if self._upstox:
            return self._upstox.place_market_order(instrument_key, quantity, "SELL")
        return False


class BreakoutEngine:
    def __init__(self) -> None:
        self.client = BreakoutMarketClient()
        self.states = {code: IndexBreakoutState(config=cfg) for code, cfg in INDEX_CONFIGS.items()}
        now = datetime.now(IST)
        for state in self.states.values():
            state.trade_day = now.date().isoformat()
            self._restore_frozen_levels(state, now.date().isoformat())
        logger.info(
            "Breakout engine started (paper=%s mock=%s entries=%s max_trades=%d lot=%d)",
            PAPER_TRADING,
            MOCK_MODE,
            ENTRIES_ENABLED,
            MAX_TRADES_PER_DAY,
            LOTS_PER_TRADE,
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
        entries_blocked = kill or now.time() >= NO_ENTRY_AFTER or now.time() < SESSION_START
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
            self._process_index(state, now, entries_blocked, block_reason)
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
            self._manage_position(state, now)
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

    def _resolve_session_open(
        self,
        state: IndexBreakoutState,
        candles: list[dict[str, float]],
        now: datetime,
        spot: float | None,
    ) -> tuple[float | None, str]:
        """Match Pine: prefer 9:15 5m candle OPEN; LTP only until that bar exists."""
        if state.session_open is not None:
            return state.session_open, state.session_open_source or "frozen"

        if now.time() < SESSION_START:
            return None, ""

        first = self._first_session_candle(candles, now.date())
        if first is not None:
            return float(first["open"]), "candle"

        if spot is not None:
            return spot, "ltp_provisional"

        return None, ""

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
        levels = compute_blr_levels(
            prev["open"],
            prev["high"],
            prev["low"],
            prev["close"],
            opening_915,
            state.config.code,
        )
        state.session_open = opening_915
        state.session_open_source = open_source
        state.prev_close = prev["close"]
        state.mid = levels.mid
        state.green = levels.green
        state.red = levels.red
        state.gap_regime = levels.gap_regime
        state.band_half = levels.band_half
        state.band_half_pct = levels.band_half_pct
        state.levels_ready = True
        src_note = "9:15 candle open" if open_source == "candle" else "provisional LTP"
        base_label = (
            f"BLR locked — G {levels.green:.2f} / M {levels.mid:.2f} / R {levels.red:.2f} "
            f"({levels.gap_regime} · {levels.band_half_pct:.3f}% half · {src_note} {opening_915:.2f})"
        )
        state.setup_label = base_label
        state.signal_log.append(base_label)
        self._save_frozen_levels(state)
        logger.info(
            "[%s] BLR locked — G %.2f / M %.2f / R %.2f (%s · %.3f%% half · %s %.2f · prevC %.2f)",
            state.config.code,
            levels.green,
            levels.mid,
            levels.red,
            levels.gap_regime,
            levels.band_half_pct,
            src_note,
            opening_915,
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
            first = self._first_session_candle(candles, now.date())
            if first is not None and state.session_open_source == "ltp_provisional":
                candle_open = float(first["open"])
                if abs(candle_open - (state.session_open or 0)) >= 0.01:
                    logger.info(
                        "[%s] Re-locking BLR from 9:15 candle open %.2f (was LTP %.2f)",
                        state.config.code,
                        candle_open,
                        state.session_open or 0,
                    )
                    state.levels_ready = False
                    state.session_open = None
                    state.session_open_source = ""
                    state.mid = state.green = state.red = None
                elif first and state.day_review == "PENDING":
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

            if state.levels_ready:
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
            state.setup_label = "Waiting for 9:15 open (5m candle or live LTP)"
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

        if state.day_review in ("", "PENDING"):
            state.setup_label = "Awaiting 9:20 5m close for day review"
            return

        if len(candles) < 2:
            return

        candle = candles[-1]
        if candle["timestamp"] == state.last_candle_ts:
            return

        prev_close = float(candles[-2]["close"])
        close = float(candle["close"])
        state.last_candle_ts = candle["timestamp"]

        direction, reason = detect_breakout_signal(
            prev_close,
            close,
            state.green,
            state.red,
            state.mid,
            state.day_review,
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
        contract = self.client.resolve_option(state.config, close, direction)
        if contract is None:
            logger.error("[%s] breakout entry aborted — no option contract", state.config.code)
            return

        quantity = state.config.lot_size * LOTS_PER_TRADE
        if not self.client.place_entry(contract.get("instrument_key", ""), quantity):
            logger.error("[%s] breakout entry order failed", state.config.code)
            return

        state.position = BreakoutPosition(
            direction=direction,
            entry_price=close,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            lot_size=state.config.lot_size,
            instrument_key=str(contract.get("instrument_key") or ""),
            option_strike=int(contract["strike"]),
            option_type=str(contract["option_type"]),
            opened_at=now.isoformat(),
            entry_reason=reason,
        )
        state.trades_today += 1
        option_label = f"{contract['strike']}{contract['option_type']}"
        msg = (
            f"{state.config.display} BREAKOUT {direction} @ {close:.2f} "
            f"via {option_label} x{LOTS_PER_TRADE} lot — {reason} "
            f"SL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f} (book @ TP1)"
        )
        state.setup_label = f"{direction} entry — {reason}"
        state.signal_log.append(msg)
        logger.info(msg)
        telegram_notifier.notify_trade_execution(
            index_name=f"{state.config.display} Breakout ({option_label} x{LOTS_PER_TRADE})",
            trade_type=direction,
            entry_price=close,
            target_price=tp1,
            sl_price=sl,
            tp2_price=tp2,
            component_sentiment=state.gap_regime,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
            candles=candles,
        )

    def _manage_position(self, state: IndexBreakoutState, now: datetime) -> None:
        pos = state.position
        if pos is None or state.spot is None:
            return

        spot = state.spot
        # Sensex: after +50 pts move SL to entry (cost)
        if state.config.code == "SENSEX":
            fav = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
            if fav >= SENSEX_COST_SL_PTS:
                if pos.direction == "LONG" and pos.sl_price < pos.entry_price:
                    pos.sl_price = pos.entry_price
                elif pos.direction == "SHORT" and pos.sl_price > pos.entry_price:
                    pos.sl_price = pos.entry_price

        exit_reason = ""
        if pos.direction == "LONG":
            if spot <= pos.sl_price:
                exit_reason = "SL"
            elif spot >= pos.tp1_price:
                exit_reason = "TP1 booked"
        else:
            if spot >= pos.sl_price:
                exit_reason = "SL"
            elif spot <= pos.tp1_price:
                exit_reason = "TP1 booked"

        if not exit_reason:
            return

        if pos.instrument_key:
            self.client.place_exit(pos.instrument_key, pos.quantity)

        pnl = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
        msg = f"{state.config.display} BREAKOUT exit {exit_reason} @ {spot:.2f} ({pnl:+.2f} pts)"
        state.signal_log.append(msg)
        logger.info(msg)
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_BREAKOUT,
            strategy_id="breakout",
            symbol=state.config.code,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=spot,
            pnl_points=pnl,
            exit_reason=exit_reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
        )
        state.setup_label = f"Flat after {exit_reason}"
        state.position = None
        telegram_notifier.notify_trade_exit(
            index_name=f"{state.config.display} Breakout ({pos.option_strike}{pos.option_type})",
            trade_type=pos.direction,
            exit_price=spot,
            pnl_points=pnl,
            reason=exit_reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _square_off_all(self, reason: str, now: datetime) -> None:
        for state in self.states.values():
            if state.position is not None:
                spot = state.spot if state.spot is not None else state.position.entry_price
                pos = state.position
                if pos.instrument_key:
                    self.client.place_exit(pos.instrument_key, pos.quantity)
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
            "session_open": state.session_open,
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
                "option_strike": pos.option_strike,
                "option_type": pos.option_type,
                "entry_reason": pos.entry_reason,
                "opened_at": pos.opened_at,
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
