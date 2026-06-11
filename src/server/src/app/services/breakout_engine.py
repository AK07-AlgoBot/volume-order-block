"""AK07 Breakout System — Strategy Type 3.

Daily Green / Mid / Red levels from previous-day range + session open (Pine BLR
logic). The first 5-minute close vs Mid sets which side to review for the session.
Breakout entries on 5m close through Green (long) or Red (short), with override
when price closes through the opposite line. Options only, 1 lot, TP1 (1R), max
2 trades/day per index.

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
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager, telegram_notifier
from app.services import performance_store
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
RR1: Final[float] = 1.0
SL_BUFFER: Final[float] = float(os.environ.get("BREAKOUT_SL_BUFFER_PTS", "2.0"))

# Pine locked BLR constants
UP_R: Final[float] = 0.08
UP_B: Final[float] = 0.30
DN_R: Final[float] = 0.26
DN_B: Final[float] = 0.10
GAP_UP_K: Final[float] = 0.06
GAP_DN_K: Final[float] = 0.10
FLAT_GAP_PCT: Final[float] = 0.10
GREEN_MUL_GAPUP: Final[float] = 0.90
RED_MUL_GAPUP: Final[float] = 1.00
GREEN_MUL_GAPDN: Final[float] = 1.00
RED_MUL_GAPDN: Final[float] = 0.90
GREEN_MUL_FLAT: Final[float] = 0.95
RED_MUL_FLAT: Final[float] = 0.95
GREEN_GAPUP_SLOPE: Final[float] = 0.08
RED_GAPDN_SLOPE: Final[float] = 0.08


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
NO_ENTRY_AFTER: Final[dtime] = _parse_ist_time("BREAKOUT_NO_ENTRY_AFTER_IST", 14, 45)
SQUARE_OFF_TIME: Final[dtime] = _parse_ist_time("BREAKOUT_SQUARE_OFF_IST", 14, 55)
SESSION_END: Final[dtime] = _parse_ist_time("BREAKOUT_SESSION_END_IST", 15, 30)


@dataclass(frozen=True)
class BLRLevels:
    mid: float
    green: float
    red: float
    gap_regime: str
    session_open: float
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
) -> BLRLevels:
    """Replicate Pine BLR level math (SessionOpen base mode)."""
    prev_range = prev_high - prev_low
    prev_body = abs(prev_close - prev_open)
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

    green_dist = UP_R * prev_range + UP_B * prev_body + (GAP_UP_K * gap_abs if is_gap_up else 0.0)
    red_dist = DN_R * prev_range + DN_B * prev_body + (GAP_DN_K * gap_abs if is_gap_dn else 0.0)

    g_mul = GREEN_MUL_FLAT if is_flat else (GREEN_MUL_GAPUP if is_gap_up else GREEN_MUL_GAPDN)
    r_mul = RED_MUL_FLAT if is_flat else (RED_MUL_GAPUP if is_gap_up else RED_MUL_GAPDN)

    if is_gap_up:
        g_mul = max(0.70, g_mul - GREEN_GAPUP_SLOPE * gap_pct)
    if is_gap_dn:
        r_mul = max(0.70, r_mul - RED_GAPDN_SLOPE * gap_pct)

    green = base + green_dist * g_mul
    red = base - red_dist * r_mul

    return BLRLevels(
        mid=base,
        green=green,
        red=red,
        gap_regime=gap_regime,
        session_open=session_open,
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
) -> tuple[str | None, str]:
    """Return (direction, reason) for a closed 5m candle breakout."""
    long_breakout = prev_close <= green and close > green and close > mid
    short_breakout = prev_close >= red and close < red and close < mid

    if long_breakout:
        if day_review in ("LONG", "NEUTRAL"):
            return "LONG", "green breakout (review side)"
        return "LONG", "green breakout override (against review)"

    if short_breakout:
        if day_review in ("SHORT", "NEUTRAL"):
            return "SHORT", "red breakdown (review side)"
        return "SHORT", "red breakdown override (against review)"

    return None, ""


def trade_levels(
    direction: str,
    entry: float,
    mid: float,
    green: float,
    red: float,
    gap_regime: str,
) -> tuple[float, float, float]:
    """Spot SL, TP1 (1R book), TP2 (2R reference)."""
    if direction == "LONG":
        sl_anchor = mid if gap_regime == "GAP_UP" else red
        sl = sl_anchor - SL_BUFFER
        risk = max(entry - sl, 0.05)
        tp1 = entry + risk * RR1
        tp2 = entry + risk * 2.0
    else:
        sl_anchor = mid if gap_regime == "GAP_DN" else green
        sl = sl_anchor + SL_BUFFER
        risk = max(sl - entry, 0.05)
        tp1 = entry - risk * RR1
        tp2 = entry - risk * 2.0
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

    def get_5m_candles(self, cfg: IndexConfig) -> list[dict[str, float]] | None:
        if MOCK_MODE:
            return self._mock_candles(cfg)
        if not self._upstox:
            return []
        v3_base = self._upstox.base_url.replace("/v2", "/v3")
        data = self._upstox._get(  # noqa: SLF001
            f"{v3_base}/historical-candle/intraday/{cfg.spot_instrument_key}/minutes/{CANDLE_5M}"
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
        v3_base = self._upstox.base_url.replace("/v2", "/v3")
        data = self._upstox._get(f"{v3_base}/historical-candle/{cfg.spot_instrument_key}/days/1")  # noqa: SLF001
        if not isinstance(data, dict):
            return None
        rows = data.get("candles") or []
        if not isinstance(rows, list):
            return None
        today = date.today()
        for row in rows:
            parsed = _parse_daily_row(row)
            if parsed and date.fromisoformat(parsed["date"]) < today:
                return parsed
        if len(rows) >= 2:
            parsed = _parse_daily_row(rows[1])
            if parsed:
                return parsed
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
                prev["open"], prev["high"], prev["low"], prev["close"], session_open=spot
            )
            self._mock_levels[cfg.code] = levels

        levels = self._mock_levels[cfg.code]
        self._tick += 1
        if self._tick == 1:
            close = levels.mid + (levels.green - levels.mid) * 0.15
        elif self._tick >= 4:
            close = levels.green + 5
        else:
            close = levels.mid + (levels.green - levels.mid) * 0.55

        ts = now - timedelta(minutes=CANDLE_5M)
        return [
            {
                "timestamp": ts.isoformat(),
                "open": close - 8,
                "high": close + 12,
                "low": close - 15,
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
        logger.info(
            "Breakout engine started (paper=%s mock=%s max_trades=%d lot=%d)",
            PAPER_TRADING,
            MOCK_MODE,
            MAX_TRADES_PER_DAY,
            LOTS_PER_TRADE,
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
            self._publish_all(now, entries_blocked=True, block_reason="session closed")
            return

        kill = self._kill_switch_engaged()
        entries_blocked = kill or now.time() >= NO_ENTRY_AFTER or now.time() < SESSION_START
        if kill:
            self._square_off_all("KILL_SWITCH", now)

        if now.time() >= SQUARE_OFF_TIME:
            self._square_off_all("SESSION_SQUARE_OFF", now)
            entries_blocked = True

        for state in self.states.values():
            self._process_index(state, now, entries_blocked)
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
                state.day_review = "PENDING"
                state.first_candle_close = None
                state.last_candle_ts = ""
                state.setup_label = "New session — building BLR levels"

    def _process_index(self, state: IndexBreakoutState, now: datetime, entries_blocked: bool) -> None:
        cfg = state.config
        if now.time() < SESSION_START:
            state.setup_label = f"Pre-market — session from {SESSION_START.strftime('%H:%M')} IST"
            self._publish_state(state, now, entries_blocked=True)
            return

        spot = self.client.get_spot(cfg)
        if spot is not None:
            state.spot = spot

        candles = self.client.get_5m_candles(cfg) or []
        self._refresh_levels(state, candles, now)

        if state.position:
            self._manage_position(state, now)
        elif (
            not entries_blocked
            and state.levels_ready
            and state.day_review not in ("PENDING",)
            and now.time() >= ENTRY_START
            and state.trades_today < MAX_TRADES_PER_DAY
            and candles
        ):
            self._seek_entry(state, candles, now)

        self._publish_state(state, now, entries_blocked)

    def _session_open_from_candles(self, candles: list[dict[str, float]], day: date) -> float | None:
        for candle in candles:
            ts = datetime.fromisoformat(candle["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.date() == day and ts.time() == SESSION_START:
                return float(candle["open"])
        for candle in candles:
            ts = datetime.fromisoformat(candle["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.date() == day and ts.time() >= SESSION_START:
                return float(candle["open"])
        return float(candles[0]["open"]) if candles else None

    def _first_session_candle(self, candles: list[dict[str, float]], day: date) -> dict[str, float] | None:
        for candle in candles:
            ts = datetime.fromisoformat(candle["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.date() == day and ts.time() == SESSION_START:
                return candle
        return None

    def _refresh_levels(
        self,
        state: IndexBreakoutState,
        candles: list[dict[str, float]],
        now: datetime,
    ) -> None:
        if state.levels_ready:
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

        session_open = self._session_open_from_candles(candles, now.date())
        if session_open is None and state.spot is not None:
            session_open = state.spot

        prev = self.client.get_previous_day_ohlc(state.config)
        if session_open is None or prev is None:
            state.setup_label = "Waiting for session open + previous day OHLC"
            return

        levels = compute_blr_levels(
            prev["open"],
            prev["high"],
            prev["low"],
            prev["close"],
            session_open,
        )
        state.mid = levels.mid
        state.green = levels.green
        state.red = levels.red
        state.gap_regime = levels.gap_regime
        state.levels_ready = True
        state.setup_label = (
            f"BLR locked — G {levels.green:.2f} / M {levels.mid:.2f} / R {levels.red:.2f} "
            f"({levels.gap_regime})"
        )
        msg = state.setup_label
        state.signal_log.append(msg)
        logger.info("[%s] %s", state.config.code, msg)

        first = self._first_session_candle(candles, now.date())
        if first:
            close = float(first["close"])
            state.first_candle_close = close
            state.day_review = day_review_from_first_close(close, levels.mid)
            state.setup_label = (
                f"Review {state.day_review} side "
                f"(1st 5m close {close:.2f} vs mid {levels.mid:.2f})"
            )

    def _seek_entry(
        self,
        state: IndexBreakoutState,
        candles: list[dict[str, float]],
        now: datetime,
    ) -> None:
        if state.mid is None or state.green is None or state.red is None:
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
            state.setup_label = f"Review {state.day_review} — watching green/red breakouts"
            return

        sl, tp1, tp2 = trade_levels(
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
            component_sentiment=state.day_review,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _manage_position(self, state: IndexBreakoutState, now: datetime) -> None:
        pos = state.position
        if pos is None or state.spot is None:
            return

        spot = state.spot
        exit_reason = ""
        if pos.direction == "LONG":
            if spot <= pos.sl_price:
                exit_reason = "SL"
            elif spot >= pos.tp1_price:
                exit_reason = "TP1 booked (1R)"
        else:
            if spot >= pos.sl_price:
                exit_reason = "SL"
            elif spot <= pos.tp1_price:
                exit_reason = "TP1 booked (1R)"

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
            "levels_ready": state.levels_ready,
            "day_review": state.day_review,
            "first_candle_close": state.first_candle_close,
            "setup_label": state.setup_label,
            "trades_today": state.trades_today,
            "max_trades": MAX_TRADES_PER_DAY,
            "entries_blocked": entries_blocked,
            "block_reason": block_reason,
            "paper_trading": PAPER_TRADING,
            "signals": state.signal_log[-10:],
            "session_end_ist": SESSION_END.strftime("%H:%M"),
            "square_off_ist": SQUARE_OFF_TIME.strftime("%H:%M"),
            "no_entry_after_ist": NO_ENTRY_AFTER.strftime("%H:%M"),
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
        cache_manager.set_json(key, payload, ttl_seconds=120)

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
