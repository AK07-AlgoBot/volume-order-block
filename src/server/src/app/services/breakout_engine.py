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
    catchup_s3_legs,
    legs_summary,
    list_live_s3_traders,
    place_s3_entries,
    place_s3_exits,
    position_legs,
    s3_uses_options,
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
# Execution: options = BUY ITM CE/PE (premium TP); futures = index FUT. BLR signals unchanged either way.
# Safer ~1:2: −15 SL / +25 TP (true 1:2 would be +30 — tracked as beyond-target when peak exceeds 25).
OPTION_PREMIUM_TP_PTS: Final[float] = float(os.environ.get("BREAKOUT_OPTION_PREMIUM_TP_PTS", "25"))
OPTION_PREMIUM_SL_PTS: Final[float] = float(os.environ.get("BREAKOUT_OPTION_PREMIUM_SL_PTS", "15"))
# Ideal 1:2 premium target (pts) — used only for "beyond ideal RR" analytics.
OPTION_IDEAL_TP_PTS: Final[float] = float(os.environ.get("BREAKOUT_OPTION_IDEAL_TP_PTS", "30"))
# Book TP when premium is within this many pts of the target (avoids missing by 1–2 pts then SL).
OPTION_TP_NEAR_PTS: Final[float] = float(os.environ.get("BREAKOUT_OPTION_TP_NEAR_PTS", "1"))
# After this much premium profit, start trailing: SL → entry+lock, then +1 SL per +1 further peak.
OPTION_BREAKEVEN_PTS: Final[float] = float(os.environ.get("BREAKOUT_OPTION_BREAKEVEN_PTS", "10"))
OPTION_TRAIL_LOCK_PTS: Final[float] = float(os.environ.get("BREAKOUT_OPTION_TRAIL_LOCK_PTS", "1"))
# Ignore exits for N seconds after entry (avoids open-print noise).
OPTION_ENTRY_GRACE_SEC: Final[float] = float(os.environ.get("BREAKOUT_OPTION_ENTRY_GRACE_SEC", "20"))
# While holding options, poll premium every N seconds (catch TP without waiting for 15s candle loop).
OPTION_POLL_SECONDS: Final[float] = float(os.environ.get("BREAKOUT_OPTION_POLL_SECONDS", "2"))
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
FRIDAY_1TO1_TP: Final[bool] = os.environ.get("BREAKOUT_FRIDAY_1TO1_TP", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)

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
# Mid auto-lock (TV parity — avoid daily manual paste):
# 1) Prefer Upstox market-quote day OHLC open — usually matches TradingView index Mid.
# 2) 5m candle open often = NSE auction (≠ TV); ignore when it equals day open.
# 3) first_ltp is fallback only when day OHLC is late/missing; never sticky over day OHLC.
# 4) manual_tv/manual_admin overrides stay highest rank for rare edge cases.
SESSION_OPEN_TICK_WINDOW_SEC: Final[int] = int(
    os.environ.get("BREAKOUT_SESSION_OPEN_TICK_WINDOW_SEC", "120")
)
# Wait for live LTP only when Mid would otherwise come from auction 5m candle.
# Default off — day OHLC locks Mid automatically without waiting for a tick.
SESSION_OPEN_TICK_WAIT: Final[bool] = os.environ.get(
    "BREAKOUT_SESSION_OPEN_WAIT_FOR_TICK", "0"
).strip().lower() in ("1", "true", "yes", "on")
SESSION_OPEN_POLL_SECONDS: Final[float] = float(
    os.environ.get("BREAKOUT_SESSION_OPEN_POLL_SECONDS", "1")
)
SESSION_OPEN_PRESTART_SEC: Final[int] = int(
    os.environ.get("BREAKOUT_SESSION_OPEN_PRESTART_SEC", "10")
)
# Only accept LTP captured within this many seconds after 9:15 (fallback Mid).
SESSION_OPEN_TICK_MAX_DELAY_SEC: Final[int] = int(
    os.environ.get("BREAKOUT_SESSION_OPEN_TICK_MAX_DELAY_SEC", "45")
)
# Upstox LTP can sit at NSE auction price for seconds; wait until it diverges when using LTP fallback.
AUCTION_LTP_TOLERANCE_PTS: Final[float] = float(
    os.environ.get("BREAKOUT_AUCTION_LTP_TOLERANCE_PTS", "1.5")
)


def _prior_session_last_5m_close(
    instrument_key: str,
    prior_day: date,
    *,
    username: str = "AK07",
) -> float | None:
    """Last 5m body close of the prior session — matches TradingView daily close[1] on index."""
    try:
        from app.services.backtest_data import HistoricalDataClient, parse_candle_ts

        client = HistoricalDataClient(username=username)
        start = prior_day - timedelta(days=5)
        candles = client.fetch_5m(instrument_key, start, prior_day, use_cache=True)
        session_bars = [
            c
            for c in candles
            if (ts := parse_candle_ts(c["timestamp"])).date() == prior_day
            and SESSION_START <= ts.time() < SESSION_END
        ]
        if not session_bars:
            return None
        last_bar = max(session_bars, key=lambda c: c["timestamp"])
        return float(last_bar["close"])
    except Exception as exc:
        logger.warning("prior session 5m close fetch failed for %s: %s", prior_day, exc)
        return None


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
# Skip breakout entries whose 5m bar closed more than this many seconds ago.
# Prevents post-restart catch-up from firing a live order at the day high for an old signal.
STALE_BREAKOUT_MAX_AGE_SEC: Final[float] = float(
    os.environ.get("BREAKOUT_STALE_SIGNAL_SEC", "360")
)


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
    exit_pending: bool = False
    instrument_kind: str = "futures"  # futures | options
    premium_entry: float | None = None  # option LTP at entry (options mode)
    premium_last: float | None = None  # last observed option LTP (tick-to-tick)
    premium_high: float | None = None  # peak premium since entry (favorable excursion)

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

    @property
    def uses_options(self) -> bool:
        return self.instrument_kind == "options" or bool(self.option_type)


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
    admin_updated_at: str = ""
    session_open_tick: float | None = None
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
    last_fanout_catchup_mono: float = 0.0
    last_candle_fetch_mono: float = 0.0
    cached_candles: list[dict[str, float]] = field(default_factory=list)


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
        "instrument_kind": pos.instrument_kind,
        "premium_entry": pos.premium_entry,
        "premium_last": pos.premium_last,
        "premium_high": pos.premium_high,
        "exit_pending": pos.exit_pending,
    }


def _position_from_dict(raw: dict[str, Any]) -> BreakoutPosition | None:
    try:
        direction = str(raw.get("direction") or "")
        if direction not in ("LONG", "SHORT"):
            return None
        entry = float(raw["entry_price"])
        sl = float(raw["sl_price"])
        tp1 = float(raw["tp1_price"])
        tp2_raw = raw.get("tp2_price")
        if tp2_raw is None:
            tp2 = entry + FIXED_TP_PTS if direction == "LONG" else entry - FIXED_TP_PTS
        else:
            tp2 = float(tp2_raw)
        lot_size = int(raw.get("lot_size") or 65)
        contract_label = str(raw.get("contract_label") or "")
        option_strike = int(raw.get("option_strike") or 0)
        option_type = str(raw.get("option_type") or "")
        if not contract_label and option_strike and option_type:
            contract_label = f"{option_strike}{option_type}"
        legs_raw = raw.get("order_legs")
        order_legs = legs_raw if isinstance(legs_raw, list) else []
        instrument_key = str(raw.get("instrument_key") or "")
        if not instrument_key and order_legs:
            instrument_key = str(order_legs[0].get("instrument_key") or "")
        if not contract_label and order_legs:
            contract_label = str(
                order_legs[0].get("contract_label") or order_legs[0].get("trading_symbol") or ""
            )
        premium_raw = raw.get("premium_entry")
        premium_entry = float(premium_raw) if premium_raw is not None else None
        last_raw = raw.get("premium_last")
        premium_last = float(last_raw) if last_raw is not None else None
        high_raw = raw.get("premium_high")
        premium_high = float(high_raw) if high_raw is not None else None
        kind = str(raw.get("instrument_kind") or ("options" if option_type else "futures"))
        return BreakoutPosition(
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            lot_size=lot_size,
            instrument_key=instrument_key,
            contract_label=contract_label,
            opened_at=str(raw.get("opened_at") or ""),
            entry_reason=str(raw.get("entry_reason") or ""),
            order_legs=[leg for leg in order_legs if isinstance(leg, dict)],
            option_strike=option_strike,
            option_type=option_type,
            exit_pending=bool(raw.get("exit_pending")),
            instrument_kind=kind,
            premium_entry=premium_entry,
            premium_last=premium_last,
            premium_high=premium_high,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _s3_trade_extra(
    pos: BreakoutPosition,
    *,
    spot_exit: float,
    exit_price: float,
) -> dict[str, Any]:
    """Rich fields for the admin S3 trade log."""
    spot_entry = float(pos.entry_price)
    if pos.direction == "LONG":
        spot_moved = float(spot_exit) - spot_entry
    else:
        spot_moved = spot_entry - float(spot_exit)
    premium_entry = pos.premium_entry
    premium_high = pos.premium_high
    points_moved: float | None = None
    if pos.uses_options and premium_entry is not None:
        peak = float(premium_high) if premium_high is not None else float(exit_price)
        points_moved = peak - float(premium_entry)
        beyond_target = max(0.0, points_moved - OPTION_PREMIUM_TP_PTS)
        beyond_ideal = max(0.0, points_moved - OPTION_IDEAL_TP_PTS)
    else:
        points_moved = spot_moved
        beyond_target = None
        beyond_ideal = None
    return {
        "instrument_kind": pos.instrument_kind,
        "contract_label": pos.display_contract,
        "option_strike": pos.option_strike or None,
        "option_type": pos.option_type or None,
        "spot_entry": spot_entry,
        "spot_exit": float(spot_exit),
        "spot_points_moved": spot_moved,
        "premium_entry": premium_entry,
        "premium_exit": float(exit_price) if pos.uses_options else None,
        "premium_high": premium_high,
        "sl_price": pos.sl_price,
        "tp_price": pos.tp1_price,
        "points_moved": points_moved,
        "beyond_target": beyond_target,
        "beyond_ideal_rr": beyond_ideal,
        "ideal_tp_pts": OPTION_IDEAL_TP_PTS if pos.uses_options else None,
    }


def _record_s3_completed_trades(
    pos: BreakoutPosition,
    *,
    symbol: str,
    entry_price: float,
    exit_price: float,
    pnl_points: float,
    exit_reason: str,
    spot_exit: float,
) -> None:
    """One performance row per live trader leg so each user sees only their PnL."""
    base_extra = _s3_trade_extra(pos, spot_exit=spot_exit, exit_price=exit_price)
    legs = [leg for leg in (pos.order_legs or []) if isinstance(leg, dict)]
    attributed: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for leg in legs:
        username = str(leg.get("username") or "").strip()
        if not username or username in seen:
            continue
        seen.add(username)
        attributed.append((username, leg))

    if not attributed:
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_BREAKOUT,
            strategy_id="breakout",
            symbol=symbol,
            direction=pos.direction,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_points=pnl_points,
            exit_reason=exit_reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
            extra=base_extra,
        )
        return

    for username, leg in attributed:
        extra = dict(base_extra)
        extra["broker"] = str(leg.get("broker") or "")
        if leg.get("premium_entry") is not None:
            try:
                extra["premium_entry"] = float(leg["premium_entry"])
            except (TypeError, ValueError):
                pass
        if leg.get("option_strike"):
            try:
                extra["option_strike"] = int(leg["option_strike"])
            except (TypeError, ValueError):
                pass
        if leg.get("option_type"):
            extra["option_type"] = str(leg["option_type"])
        label = str(leg.get("contract_label") or leg.get("trading_symbol") or "")
        if label:
            extra["contract_label"] = label
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_BREAKOUT,
            strategy_id="breakout",
            symbol=symbol,
            direction=pos.direction,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_points=pnl_points,
            exit_reason=exit_reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
            username=username,
            extra=extra,
        )


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


def _candle_ts_key(raw: str) -> str:
    """Normalize candle timestamps so Redis cursor matches Upstox feed after restart."""
    try:
        return parse_candle_ts(raw).astimezone(IST).isoformat()
    except (TypeError, ValueError):
        return str(raw or "")


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
    *,
    session_date: date | None = None,
) -> tuple[float, float, float]:
    """Spot SL, TP1, TP2 from BREAKOUT_SIZING_MODE.

    fixed_sl_tp — entry ± FIXED_SL_PTS / FIXED_TP_PTS (default 30 / 60, 1:2 R:R).
    On Fridays (IST session), TP1 uses 1:1 (same pts as SL) when BREAKOUT_FRIDAY_1TO1_TP=1.
    band — SL at band_half + buffer, TP at 1.5× / 3× band (legacy production).
    """
    if SIZING_MODE in ("fixed", "fixed_sl_tp", "fixed_sl_and_tp"):
        sl_dist = FIXED_SL_PTS
        tp1_pts = FIXED_TP_PTS
        tp2_pts = FIXED_TP_PTS * 2
        if FRIDAY_1TO1_TP and session_date is not None and session_date.weekday() == 4:
            tp1_pts = sl_dist
            tp2_pts = FIXED_TP_PTS
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
            # Live: never invent spot from mock seed (was returning ~23100 and breaking strikes).
            logger.warning("[%s] spot LTP unavailable — skipping tick (no mock fallback)", cfg.code)
            return None
        return None

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
            last_5m_close = _prior_session_last_5m_close(
                cfg.spot_instrument_key,
                best_day,
            )
            if last_5m_close is not None:
                daily_close = float(best["close"])
                best["close"] = last_5m_close
                best["prev_close_source"] = "5m_last"
                if abs(last_5m_close - daily_close) >= 0.01:
                    logger.info(
                        "[%s] prev close from last 5m bar %.2f (daily API %.2f)",
                        cfg.code,
                        last_5m_close,
                        daily_close,
                    )
            else:
                best["prev_close_source"] = "daily"
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
        self._last_pnl_refresh_mono = 0.0
        self.states = {code: IndexBreakoutState(config=cfg) for code, cfg in INDEX_CONFIGS.items()}
        now = datetime.now(IST)
        for state in self.states.values():
            state.trade_day = now.date().isoformat()
            self._restore_session_open_tick(state, now)
            self._restore_frozen_levels(state, now.date().isoformat())
            self._invalidate_auction_frozen(state, now)
            self._restore_session_state(state, now.date().isoformat())
            self._reconcile_session_position_if_flat(state, now.date().isoformat())
        traders = list_live_s3_traders()
        logger.info(
            "Breakout engine started (paper=%s mock=%s entries=%s indices=%s sizing=%s sl=%.0f tp=%.0f friday_1to1=%s exec=%s opt_tp=%.0f opt_poll=%.0fs max_trades=%d no_entry_after=%s lot=%d live_traders=%s)",
            PAPER_TRADING,
            MOCK_MODE,
            ENTRIES_ENABLED,
            ",".join(sorted(ENTRIES_INDICES)),
            SIZING_MODE,
            FIXED_SL_PTS,
            FIXED_TP_PTS,
            FRIDAY_1TO1_TP,
            "options" if s3_uses_options() else "futures",
            OPTION_PREMIUM_TP_PTS,
            OPTION_POLL_SECONDS,
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

    def _has_open_options_position(self) -> bool:
        return any(s.position is not None and s.position.uses_options for s in self.states.values())

    def _poll_interval_seconds(self, now: datetime) -> float:
        if self._in_session_open_fast_poll(now):
            return SESSION_OPEN_POLL_SECONDS
        # Holding options → 2s premium ticks so TP fires without waiting for the 15s loop.
        if self._has_open_options_position():
            return max(1.0, OPTION_POLL_SECONDS)
        return POLL_SECONDS

    def _session_open_deadline(self, day: date) -> datetime:
        return datetime.combine(day, SESSION_START, tzinfo=IST) + timedelta(
            seconds=SESSION_OPEN_TICK_WINDOW_SEC
        )

    def _session_open_fast_poll_start(self, day: date) -> datetime:
        return datetime.combine(day, SESSION_START, tzinfo=IST) - timedelta(
            seconds=SESSION_OPEN_PRESTART_SEC
        )

    def _session_open_tick_deadline(self, day: date) -> datetime:
        return datetime.combine(day, SESSION_START, tzinfo=IST) + timedelta(
            seconds=SESSION_OPEN_TICK_MAX_DELAY_SEC
        )

    def _in_session_open_fast_poll(self, now: datetime) -> bool:
        day = now.date()
        return self._session_open_fast_poll_start(day) <= now < self._session_open_deadline(day)

    def _in_session_open_capture_window(self, now: datetime) -> bool:
        if now.time() < SESSION_START:
            return False
        return now < self._session_open_deadline(now.date())

    def _in_session_open_tick_capture_phase(self, now: datetime) -> bool:
        if now.time() < SESSION_START:
            return False
        return now < self._session_open_tick_deadline(now.date())

    def _is_auction_open(self, candle_open: float, day_open: float | None) -> bool:
        if day_open is None:
            return False
        return abs(candle_open - day_open) < 0.01

    def _ltp_still_auction(self, ltp: float, day_open: float | None) -> bool:
        if day_open is None:
            return False
        return abs(ltp - day_open) < AUCTION_LTP_TOLERANCE_PTS

    def _invalidate_auction_frozen(self, state: IndexBreakoutState, now: datetime) -> None:
        """Drop BLR locked on auction 5m candle when market-quote day open (or TV tick) exists."""
        if not state.levels_ready:
            return
        source = str(state.session_open_source or "")
        mid = state.mid
        if mid is None:
            return
        # Never override a manual TV Mid; day_ohlc is already the preferred auto source.
        if source in ("manual_tv", "manual_admin", "day_ohlc"):
            return

        day_open = self.client.get_session_day_open(state.config)
        if day_open is not None and source in ("candle", "first_ltp", "ltp_provisional", "frozen"):
            if abs(mid - day_open) >= 0.01:
                logger.warning(
                    "[%s] Clearing frozen BLR mid %.2f (%s) — day OHLC open %.2f available (TV Mid)",
                    state.config.code,
                    mid,
                    source,
                    day_open,
                )
                day = state.trade_day or now.date().isoformat()
                cache_manager.delete_key(self._frozen_key(day, state.config.code))
                state.levels_ready = False
                state.mid = state.green = state.red = None
                state.session_open = None
                state.broker_session_open = None
                state.session_open_source = ""
                if state.position is None:
                    state.last_candle_ts = ""
                return

        # Legacy: tick beat auction candle when day OHLC still missing.
        if state.session_open_tick is None:
            return
        if source not in ("candle", "frozen") or abs(mid - state.session_open_tick) < 0.01:
            return
        logger.warning(
            "[%s] Clearing frozen BLR mid %.2f (%s) — TV tick %.2f available",
            state.config.code,
            mid,
            source,
            state.session_open_tick,
        )
        day = state.trade_day or now.date().isoformat()
        cache_manager.delete_key(self._frozen_key(day, state.config.code))
        state.levels_ready = False
        state.mid = state.green = state.red = None
        state.session_open = None
        state.broker_session_open = None
        state.session_open_source = ""
        if state.position is None:
            state.last_candle_ts = ""

    def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                logger.exception("Breakout tick failed: %s", exc)
            now = datetime.now(IST)
            interval = self._poll_interval_seconds(now)
            time.sleep(max(0.5, interval - (time.monotonic() - started)))

    def tick(self) -> None:
        now = datetime.now(IST)
        self.client.refresh_token()
        self._roll_trade_day(now)
        for state in self.states.values():
            self._sync_admin_levels(state, now.date().isoformat())

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
        self._refresh_live_trader_pnl(now)
        self._publish_heartbeat(now)

    def _refresh_live_trader_pnl(self, now: datetime) -> None:
        if PAPER_TRADING or MOCK_MODE:
            return
        if now.time() < SESSION_START or now.time() >= SESSION_END:
            return
        if time.monotonic() - self._last_pnl_refresh_mono < 30.0:
            return
        self._last_pnl_refresh_mono = time.monotonic()

        from app.services.broker_pnl_store import publish_groww_pnl_snapshot
        from app.services.daily_profit_guard import publish_upstox_pnl_snapshot
        from app.services.groww_engine import GrowwClient
        from app.services.upstox_engine import build_upstox_client

        for trader in list_live_s3_traders():
            try:
                if trader.broker == "groww":
                    pnl = GrowwClient(trader.username).get_fno_day_pnl()
                    if pnl is not None:
                        publish_groww_pnl_snapshot(trader.username, pnl)
                elif trader.broker == "upstox":
                    upstox = build_upstox_client(trader.username)
                    if upstox is not None:
                        upstox.refresh_access_token_from_disk()
                        pnl = upstox.get_portfolio_day_pnl()
                        if pnl is not None:
                            publish_upstox_pnl_snapshot(pnl)
            except Exception as exc:
                logger.warning("[%s] broker P&L refresh failed: %s", trader.username, exc)

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
                state.session_open_tick = None
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

        self._reconcile_session_position_if_flat(state, now.date().isoformat())

        spot = self.client.get_spot(cfg)
        if spot is not None:
            state.spot = spot
        self._restore_session_open_tick(state, now)
        self._maybe_capture_session_open_tick(state, now, spot)
        self._invalidate_auction_frozen(state, now)

        # Holding options: refresh option premium every ~2s; reuse candles most ticks
        # so we don't hammer Upstox historical on every premium check.
        holding_options = state.position is not None and state.position.uses_options
        need_candles = (
            not holding_options
            or not state.cached_candles
            or (time.monotonic() - state.last_candle_fetch_mono) >= POLL_SECONDS
        )
        if need_candles:
            state.cached_candles = self.client.get_5m_candles(cfg) or []
            state.last_candle_fetch_mono = time.monotonic()
            self._refresh_levels(state, state.cached_candles, now, spot)
        candles = state.cached_candles

        if state.position:
            self._catchup_missing_fanout_legs(state)
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

    # Higher rank wins. day_ohlc ≈ TV index Mid; first_ltp is fallback only.
    _SESSION_OPEN_RANK: Final[dict[str, int]] = {
        "ltp_provisional": 0,
        "candle": 1,
        "first_ltp": 2,
        "day_ohlc": 3,
        "manual_tv": 4,
        "manual_admin": 5,
    }

    def _open_tick_key(self, day: str, index_code: str) -> str:
        return cache_manager.BREAKOUT_OPEN_TICK_KEY_TEMPLATE.format(day=day, index=index_code)

    def _restore_session_open_tick(self, state: IndexBreakoutState, now: datetime) -> None:
        if state.session_open_tick is not None:
            return
        day = state.trade_day or now.date().isoformat()
        raw = cache_manager.get_json(self._open_tick_key(day, state.config.code))
        if isinstance(raw, dict) and raw.get("price") is not None:
            state.session_open_tick = float(raw["price"])

    def _save_session_open_tick(self, state: IndexBreakoutState, now: datetime) -> None:
        if state.session_open_tick is None:
            return
        day = state.trade_day or now.date().isoformat()
        cache_manager.set_json(
            self._open_tick_key(day, state.config.code),
            {
                "price": state.session_open_tick,
                "captured_at": now.isoformat(),
            },
            ttl_seconds=86_400 * 2,
        )

    def _maybe_capture_session_open_tick(
        self,
        state: IndexBreakoutState,
        now: datetime,
        spot: float | None,
    ) -> None:
        if state.session_open_tick is not None or spot is None:
            return
        if not self._in_session_open_tick_capture_phase(now):
            return
        day_open = self.client.get_session_day_open(state.config)
        if self._ltp_still_auction(float(spot), day_open):
            return
        state.session_open_tick = float(spot)
        self._save_session_open_tick(state, now)
        logger.info(
            "[%s] session open first LTP captured %.2f at %s (TV parity — diverged from auction %.2f)",
            state.config.code,
            state.session_open_tick,
            now.strftime("%H:%M:%S"),
            day_open if day_open is not None else 0.0,
        )

    def _best_session_open(
        self,
        state: IndexBreakoutState,
        candles: list[dict[str, float]],
        now: datetime,
        spot: float | None,
    ) -> tuple[float | None, str]:
        """Prefer market-quote day open (TV Mid). Auction 5m candle last; LTP only as fallback."""
        if now.time() < SESSION_START:
            return None, ""

        if state.session_open_source in ("manual_tv", "manual_admin") and state.broker_session_open is not None:
            return state.broker_session_open, state.session_open_source

        day_open = self.client.get_session_day_open(state.config)
        if day_open is not None:
            return float(day_open), "day_ohlc"

        if state.session_open_tick is not None:
            return state.session_open_tick, "first_ltp"

        first = self._first_session_candle(candles, now.date())
        if first is not None:
            candle_open = float(first["open"])
            # Without day OHLC we cannot tell auction vs regular; still better than nothing.
            return candle_open, "candle"

        if ALLOW_PROVISIONAL_LTP and spot is not None:
            return spot, "ltp_provisional"
        return None, ""

    def _session_open_source_rank(self, source: str) -> int:
        return self._SESSION_OPEN_RANK.get(source, -1)

    def _should_upgrade_session_open(
        self,
        cur_source: str,
        cur_open: float,
        new_open: float,
        new_source: str,
    ) -> bool:
        if cur_source in ("manual_tv", "manual_admin"):
            return False
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
        levels = compute_blr_levels(
            prev["open"],
            prev["high"],
            prev["low"],
            prev["close"],
            opening_915,
            state.config.code,
        )
        state.broker_session_open = opening_915
        state.session_open_tv_offset = 0.0
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
        src_notes = {
            "candle": "9:15 candle open",
            "first_ltp": "9:15 first tick",
            "day_ohlc": "NSE day open (TV Mid)",
            "manual_tv": "manual TV Mid",
            "manual_admin": "admin BLR",
            "ltp_provisional": "provisional LTP",
        }
        src_note = src_notes.get(open_source, open_source or "session open")
        prev_src = str(prev.get("prev_close_source") or "daily")
        prev_note = f"prevC {prev['close']:.2f}"
        if prev_src == "5m_last":
            prev_note = f"prevC {prev['close']:.2f} (last 5m)"
        open_note = f"{src_note} {opening_915:.2f} · {prev_note}"
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
            cur_source = state.session_open_source or ""
            # Already on best auto Mid (or manual) — do not re-hit /market-quote/ohlc every poll.
            if cur_source in ("day_ohlc", "manual_tv", "manual_admin"):
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

            best_open, best_source = self._best_session_open(state, candles, now, spot)
            cur_broker = state.broker_session_open
            if cur_broker is None and state.session_open is not None:
                cur_broker = state.session_open - state.session_open_tv_offset
            cur_broker = cur_broker or 0.0
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
                # Re-scan session bars (esp. first 9:15 close) against new Green/Red.
                if state.position is None:
                    state.last_candle_ts = ""
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
            if now.time() >= SESSION_START and now >= self._session_open_tick_deadline(now.date()):
                state.setup_label = (
                    "BLR blocked — no day OHLC / 9:15 tick yet; "
                    "relock_blr_session_open.py with TV mid only if auto Mid never arrives"
                )
            else:
                state.setup_label = "Waiting for 9:15 session open (day OHLC)"
            return
        if (
            SESSION_OPEN_TICK_WAIT
            and open_source == "candle"
            and state.session_open_tick is None
            and self._in_session_open_capture_window(now)
        ):
            state.setup_label = "Waiting for 9:15 first tick (5m candle may be auction open)"
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

    def _reconcile_session_position_if_flat(self, state: IndexBreakoutState, day: str) -> bool:
        """Recover in-memory position when session Redis still has an open leg."""
        if state.position is not None:
            return False
        raw = cache_manager.get_json(self._session_key(day, state.config.code))
        if not isinstance(raw, dict):
            return False
        pos_raw = raw.get("position")
        if not isinstance(pos_raw, dict):
            return False
        pos = _position_from_dict(pos_raw)
        if pos is None:
            logger.error("[%s] session has position blob but parse failed — check Redis", state.config.code)
            return False
        state.position = pos
        state.trades_today = max(state.trades_today, int(raw.get("trades_today") or 0))
        logs = raw.get("signal_log")
        if isinstance(logs, list) and logs:
            state.signal_log = [str(line) for line in logs[-20:]]
        state.setup_label = (
            f"{pos.direction} open — reconciled from session "
            f"{pos.display_contract} @ {pos.entry_price:.2f}"
        )
        logger.warning(
            "[%s] reconciled open position from session (engine was flat): %s %s @ %.2f SL %.2f TP1 %.2f",
            state.config.code,
            pos.direction,
            pos.display_contract,
            pos.entry_price,
            pos.sl_price,
            pos.tp1_price,
        )
        return True

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
                "admin_updated_at": state.admin_updated_at,
            },
            ttl_seconds=86_400 * 2,
        )

    def _sync_admin_levels(self, state: IndexBreakoutState, day: str) -> bool:
        """Hot-load an admin BLR override without restarting the engine."""
        frozen = cache_manager.get_json(self._frozen_key(day, state.config.code))
        if not isinstance(frozen, dict):
            return False
        if str(frozen.get("session_open_source") or "") != "manual_admin":
            return False
        updated_at = str(frozen.get("admin_updated_at") or "")
        if not updated_at or updated_at == state.admin_updated_at:
            return False
        if frozen.get("mid") is None or frozen.get("green") is None or frozen.get("red") is None:
            return False

        state.mid = float(frozen["mid"])
        state.green = float(frozen["green"])
        state.red = float(frozen["red"])
        state.gap_regime = str(frozen.get("gap_regime") or "MANUAL")
        state.band_half = float(frozen.get("band_half") or 0)
        state.band_half_pct = float(frozen.get("band_half_pct") or 0)
        state.session_open = float(frozen.get("session_open") or state.mid)
        state.broker_session_open = float(frozen.get("broker_session_open") or state.mid)
        state.session_open_tv_offset = float(frozen.get("session_open_tv_offset") or 0)
        state.session_open_source = "manual_admin"
        state.prev_close = frozen.get("prev_close")
        state.day_review = str(frozen.get("day_review") or state.day_review)
        if frozen.get("first_candle_close") is not None:
            state.first_candle_close = float(frozen["first_candle_close"])
        state.admin_updated_at = updated_at
        state.levels_ready = True
        state.setup_label = (
            f"BLR updated by admin — G {state.green:.2f} / M {state.mid:.2f} / "
            f"R {state.red:.2f} (review {state.day_review})"
        )
        logger.warning(
            "[%s] hot-loaded admin BLR override G %.2f / M %.2f / R %.2f",
            state.config.code,
            state.green,
            state.mid,
            state.red,
        )
        return True

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
        state.admin_updated_at = str(frozen.get("admin_updated_at") or "")
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

        if not candles:
            return

        # Session bars only (9:15+). Catch up from last scanned bar so the first
        # 9:15–9:20 close is evaluated even when it is the only closed candle
        # (previously required len>=2 and skipped the first-bar breakout).
        session_candles: list[dict[str, float]] = []
        for candle in candles:
            ts = parse_candle_ts(candle["timestamp"])
            if ts.date() == now.date() and ts.time() >= SESSION_START:
                session_candles.append(candle)
        if not session_candles:
            return

        start_idx = 0
        if state.last_candle_ts:
            last_key = _candle_ts_key(state.last_candle_ts)
            found_cursor = False
            for i, candle in enumerate(session_candles):
                if (
                    candle["timestamp"] == state.last_candle_ts
                    or _candle_ts_key(candle["timestamp"]) == last_key
                ):
                    start_idx = i + 1
                    found_cursor = True
                    break
            if not found_cursor:
                # Do not replay the whole session after a restart / timestamp format change.
                # Only evaluate the latest closed bar (stale guard still applies).
                logger.warning(
                    "[%s] last_candle_ts %s not in feed — skipping full-day catch-up replay",
                    state.config.code,
                    state.last_candle_ts,
                )
                start_idx = max(0, len(session_candles) - 1)

        direction: str | None = None
        reason = ""
        close = 0.0
        for i in range(start_idx, len(session_candles)):
            candle = session_candles[i]
            candle_ts = parse_candle_ts(candle["timestamp"])
            if candle_ts.time() > NO_ENTRY_AFTER:
                state.setup_label = (
                    f"Past {NO_ENTRY_AFTER.strftime('%H:%M')} — no new entries "
                    f"(flat {SQUARE_OFF_TIME.strftime('%H:%M')})"
                )
                return

            close = float(candle["close"])
            state.last_candle_ts = candle["timestamp"]

            if i == 0:
                first_bar = True
                prev_close = float(candle["open"])
            else:
                prev_candle = session_candles[i - 1]
                prev_close = float(prev_candle["close"])
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
                continue

            # 5m bar timestamp is bar open; signal is valid at bar close.
            bar_close_at = candle_ts + timedelta(minutes=CANDLE_5M)
            age_sec = (now - bar_close_at).total_seconds()
            if STALE_BREAKOUT_MAX_AGE_SEC > 0 and age_sec > STALE_BREAKOUT_MAX_AGE_SEC:
                logger.warning(
                    "[%s] skip stale %s breakout on %s (bar closed %.0fs ago) — "
                    "catch-up only, not a live entry",
                    state.config.code,
                    direction,
                    candle_ts.strftime("%H:%M"),
                    age_sec,
                )
                reason = f"stale {direction} breakout skipped ({age_sec:.0f}s old)"
                direction = None
                continue
            break
        else:
            blocked = reason or f"Watching breakouts (Review {state.day_review} day filter)"
            state.setup_label = blocked
            self._save_session_state(state)
            return

        if direction is None:
            self._save_session_state(state)
            return

        sl, tp1, tp2 = trade_levels(
            state.config.code,
            direction,
            close,
            state.mid,
            state.green,
            state.red,
            state.gap_regime,
            session_date=now.date(),
        )
        uses_options = s3_uses_options()
        contract = self.client.resolve_future(state.config)
        if contract is None and not uses_options:
            logger.error("[%s] breakout entry aborted — no futures contract", state.config.code)
            return

        legs = place_s3_entries(
            index_code=state.config.code,
            direction=direction,
            lot_size=state.config.lot_size,
            lots=LOTS_PER_TRADE,
            upstox_market_client=self.client._upstox,
            global_paper=PAPER_TRADING or MOCK_MODE,
            spot=close,
        )
        if not legs:
            logger.error("[%s] breakout entry aborted — no broker orders placed", state.config.code)
            return

        primary = legs[0]
        contract_label = str(
            primary.get("contract_label")
            or (contract or {}).get("contract_label")
            or f"{state.config.code} FUT"
        )
        primary_key = str(primary.get("instrument_key") or (contract or {}).get("instrument_key") or "")
        option_strike = int(primary.get("option_strike") or 0)
        option_type = str(primary.get("option_type") or "")
        premium_entry: float | None = None
        if uses_options:
            premiums = [
                float(leg["premium_entry"])
                for leg in legs
                if leg.get("premium_entry") is not None
            ]
            if premiums:
                premium_entry = sum(premiums) / len(premiums)
            elif primary_key and self.client._upstox and not primary_key.startswith("groww:"):
                premium_entry = self.client._upstox.get_ltp(primary_key)
            if premium_entry is not None and premium_entry > 0:
                # Options: TP/SL on premium; spot SL from trade_levels is replaced.
                tp1 = round(premium_entry + OPTION_PREMIUM_TP_PTS, 2)
                tp2 = round(premium_entry + OPTION_IDEAL_TP_PTS, 2)
                sl = round(max(premium_entry - OPTION_PREMIUM_SL_PTS, 0.05), 2)
            else:
                logger.warning(
                    "[%s] option premium unavailable after entry — using spot TP until LTP arrives",
                    state.config.code,
                )

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
            option_strike=option_strike,
            option_type=option_type,
            instrument_kind="options" if uses_options else "futures",
            premium_entry=premium_entry,
            premium_last=premium_entry,
            premium_high=premium_entry,
        )
        state.trades_today += 1
        if uses_options and premium_entry is not None:
            msg = (
                f"{state.config.display} BREAKOUT {direction} @ spot {close:.2f} "
                f"via {contract_label} x{LOTS_PER_TRADE} lot — {reason} "
                f"[{fanout_note}] "
                f"premium {premium_entry:.2f} TP {tp1:.2f} (+{OPTION_PREMIUM_TP_PTS:.0f}) "
                f"SL {sl:.2f} (−{OPTION_PREMIUM_SL_PTS:.0f})"
            )
        else:
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
            entry_price=premium_entry if (uses_options and premium_entry) else close,
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

    def _catchup_missing_fanout_legs(self, state: IndexBreakoutState) -> None:
        """Retry S3 entry for live traders who missed the initial fan-out (e.g. Groww ref error)."""
        pos = state.position
        if pos is None or PAPER_TRADING or MOCK_MODE:
            return
        now_mono = time.monotonic()
        if now_mono - state.last_fanout_catchup_mono < 60.0:
            return
        state.last_fanout_catchup_mono = now_mono

        existing = list(pos.order_legs or [])
        new_legs = catchup_s3_legs(
            index_code=state.config.code,
            direction=pos.direction,
            lot_size=state.config.lot_size,
            lots=LOTS_PER_TRADE,
            existing_legs=existing,
            upstox_market_client=self.client._upstox,
            global_paper=False,
            spot=state.spot or pos.entry_price,
        )
        if not new_legs:
            return
        pos.order_legs = existing + new_legs
        note = legs_summary(new_legs)
        msg = f"{state.config.display} S3 catch-up entry [{note}]"
        state.signal_log.append(msg)
        logger.info(msg)
        self._save_session_state(state)

    def _option_premium_ltp(self, pos: BreakoutPosition) -> float | None:
        """Live option LTP from Upstox (primary) or Groww leg symbol."""
        key = pos.instrument_key
        if key and not key.startswith("groww:") and self.client._upstox:
            ltp = self.client._upstox.get_ltp(key)
            if ltp is not None:
                return float(ltp)
        for leg in pos.order_legs or []:
            if not isinstance(leg, dict):
                continue
            if leg.get("broker") == "groww" and leg.get("trading_symbol"):
                from app.services.groww_engine import GrowwClient

                groww = GrowwClient(str(leg.get("username") or "Nani"))
                ltp = groww.get_fno_ltp(str(leg["trading_symbol"]))
                if ltp is not None:
                    return float(ltp)
            ik = str(leg.get("instrument_key") or "")
            if ik and not ik.startswith("groww:") and self.client._upstox:
                ltp = self.client._upstox.get_ltp(ik)
                if ltp is not None:
                    return float(ltp)
        return None

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
        if state.config.code == "SENSEX" and not pos.uses_options:
            fav = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
            if fav >= SENSEX_COST_SL_PTS:
                if pos.direction == "LONG" and pos.sl_price < pos.entry_price:
                    pos.sl_price = pos.entry_price
                elif pos.direction == "SHORT" and pos.sl_price > pos.entry_price:
                    pos.sl_price = pos.entry_price

        exit_reason = ""
        exit_price = spot

        if pos.uses_options:
            premium = self._option_premium_ltp(pos)
            if premium is None and pos.premium_last is not None:
                # Upstox 429 / LTP gaps: still evaluate SL/TP against last known premium
                # so positions can exit instead of running naked.
                premium = float(pos.premium_last)
                logger.warning(
                    "[%s] option LTP missing — using last premium %.2f for SL/TP check",
                    state.config.code,
                    premium,
                )
            elif premium is None:
                logger.error(
                    "[%s] option LTP unavailable and no last premium — cannot exit this tick",
                    state.config.code,
                )
            prev_premium = pos.premium_last
            if pos.premium_entry is None and premium is not None and premium > 0:
                pos.premium_entry = premium
                pos.tp1_price = round(premium + OPTION_PREMIUM_TP_PTS, 2)
                pos.tp2_price = round(premium + OPTION_IDEAL_TP_PTS, 2)
                # Re-purpose sl_price as premium floor when in options mode.
                pos.sl_price = round(max(premium - OPTION_PREMIUM_SL_PTS, 0.05), 2)
                pos.premium_high = premium
                logger.info(
                    "[%s] option premium captured %.2f → TP %.2f / SL %.2f (−%.0f / +%.0f; ideal +%.0f)",
                    state.config.code,
                    premium,
                    pos.tp1_price,
                    pos.sl_price,
                    OPTION_PREMIUM_SL_PTS,
                    OPTION_PREMIUM_TP_PTS,
                    OPTION_IDEAL_TP_PTS,
                )
                self._save_session_state(state)

            # Grace: open prints can whip; don't exit on first N seconds.
            in_grace = False
            if pos.opened_at and OPTION_ENTRY_GRACE_SEC > 0:
                try:
                    opened = datetime.fromisoformat(pos.opened_at)
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=IST)
                    in_grace = (now - opened).total_seconds() < OPTION_ENTRY_GRACE_SEC
                except ValueError:
                    in_grace = False

            tp_hit = False
            sl_hit = False
            if premium is not None:
                if pos.premium_high is None or premium > pos.premium_high:
                    pos.premium_high = premium
                # Stepped trail: after +10 → SL at entry+1; each further +1 peak → SL +1.
                if (
                    pos.premium_entry is not None
                    and OPTION_BREAKEVEN_PTS > 0
                    and pos.premium_high is not None
                ):
                    peak_profit = float(pos.premium_high) - float(pos.premium_entry)
                    if peak_profit + 1e-9 >= OPTION_BREAKEVEN_PTS:
                        steps_beyond = int(peak_profit - OPTION_BREAKEVEN_PTS)
                        new_sl = round(
                            float(pos.premium_entry)
                            + OPTION_TRAIL_LOCK_PTS
                            + max(0, steps_beyond),
                            2,
                        )
                        # Never trail above TP − 0.05 (leave room to book target).
                        new_sl = min(new_sl, round(pos.tp1_price - 0.05, 2))
                        if new_sl > pos.sl_price + 0.009:
                            pos.sl_price = new_sl
                            logger.info(
                                "[%s] option SL trailed to %.2f (peak +%.1f ≥ +%.0f → lock +%.0f then +1/pt)",
                                state.config.code,
                                pos.sl_price,
                                peak_profit,
                                OPTION_BREAKEVEN_PTS,
                                OPTION_TRAIL_LOCK_PTS,
                            )
                            self._save_session_state(state)

                # Book slightly early so a 1–2 pt miss of exact TP doesn't reverse into SL.
                tp_trigger = pos.tp1_price - max(0.0, OPTION_TP_NEAR_PTS)
                if premium >= tp_trigger:
                    tp_hit = True
                elif prev_premium is not None and prev_premium < tp_trigger <= premium:
                    tp_hit = True
                if pos.premium_entry is not None:
                    if premium <= pos.sl_price:
                        sl_hit = True
                    elif prev_premium is not None and prev_premium > pos.sl_price >= premium:
                        sl_hit = True
                if prev_premium is None or abs(premium - prev_premium) >= 0.05:
                    logger.debug(
                        "[%s] option premium tick %.2f → %.2f (TP %.2f SL %.2f)",
                        state.config.code,
                        prev_premium if prev_premium is not None else premium,
                        premium,
                        pos.tp1_price,
                        pos.sl_price,
                    )
                pos.premium_last = premium

            if in_grace and not tp_hit:
                return

            if tp_hit:
                near_note = (
                    f" near −{OPTION_TP_NEAR_PTS:.0f}"
                    if OPTION_TP_NEAR_PTS > 0
                    else ""
                )
                exit_reason = f"TP1 booked (+{OPTION_PREMIUM_TP_PTS:.0f} prem{near_note})"
                exit_price = float(premium) if premium is not None else pos.tp1_price
            elif sl_hit:
                exit_reason = f"SL (−{OPTION_PREMIUM_SL_PTS:.0f} prem / trail)"
                exit_price = float(premium) if premium is not None else pos.sl_price
        elif pos.direction == "LONG":
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

        if pos.exit_pending:
            return

        pos.exit_pending = True
        if not place_s3_exits(position_legs(pos), pos.direction, global_paper=PAPER_TRADING or MOCK_MODE):
            pos.exit_pending = False
            logger.error("[%s] breakout exit order failed (%s)", state.config.code, exit_reason)
            return

        if pos.uses_options and pos.premium_entry is not None:
            pnl = exit_price - pos.premium_entry
        else:
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
        _record_s3_completed_trades(
            pos,
            symbol=state.config.code,
            entry_price=pos.premium_entry if (pos.uses_options and pos.premium_entry) else pos.entry_price,
            exit_price=exit_price,
            pnl_points=pnl,
            exit_reason=exit_reason,
            spot_exit=spot,
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
                if pos.uses_options and pos.premium_entry is not None:
                    premium = self._option_premium_ltp(pos)
                    exit_px = float(premium) if premium is not None else float(pos.premium_last or pos.premium_entry)
                    pnl = exit_px - pos.premium_entry
                    entry_px = pos.premium_entry
                else:
                    exit_px = spot
                    entry_px = pos.entry_price
                    pnl = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
                _record_s3_completed_trades(
                    pos,
                    symbol=state.config.code,
                    entry_price=entry_px,
                    exit_price=exit_px,
                    pnl_points=pnl,
                    exit_reason=reason,
                    spot_exit=spot,
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
            "exec_instrument": "options" if s3_uses_options() else "futures",
            "option_premium_tp_pts": OPTION_PREMIUM_TP_PTS,
            "session_open": state.session_open,
            "broker_session_open": state.broker_session_open,
            "session_open_tv_offset": state.session_open_tv_offset,
            "session_open_source": state.session_open_source,
            "admin_updated_at": state.admin_updated_at,
            "session_open_tick": state.session_open_tick,
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
                "instrument_kind": pos.instrument_kind,
                "premium_entry": pos.premium_entry,
                "premium_last": pos.premium_last,
                "instrument_key": pos.instrument_key,
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
                "exec_instrument": "options" if s3_uses_options() else "futures",
                "option_premium_tp_pts": OPTION_PREMIUM_TP_PTS,
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
