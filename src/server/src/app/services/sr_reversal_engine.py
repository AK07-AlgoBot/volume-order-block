"""Strategy Type 6 — Intraday S/R Reversal.

At 09:15 IST, anchor levels = last confirmed 1H swing high/low before today
(structural pivots on the hourly chart — not yesterday's last hour candle).

Also tracks today's session OR, session range, and intraday 1H swings as they form.

Nifty · BankNifty · Sensex · 1 lot · book @ TP1 (1R) · flat 14:55 IST.

Run: python -u src/server/src/app/services/sr_reversal_engine.py
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

from app.services import cache_manager, performance_store, telegram_notifier
from app.services.engine_intraday import kill_switch_engaged, parse_ist_time, rr_book_targets
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    ITM_OFFSET_POINTS,
    IndexConfig,
    MOCK_MODE,
    PAPER_TRADING,
    UpstoxClient,
    _parse_v3_candle_row,
    build_upstox_client,
    parse_v3_intraday_candles,
)

logger = logging.getLogger("ak07.sr_reversal_engine")

IST: Final = ZoneInfo("Asia/Kolkata")
POLL_SECONDS: Final[float] = float(os.environ.get("SR_POLL_SECONDS", "15"))
CANDLE_5M: Final[int] = 5
LOTS_PER_TRADE: Final[int] = 1
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("SR_MAX_TRADES_PER_DAY", "2"))
WICK_RATIO: Final[float] = float(os.environ.get("SR_WICK_RATIO", "0.42"))
VOL_SPIKE: Final[float] = float(os.environ.get("SR_VOL_SPIKE", "1.15"))
SL_BUFFER: Final[float] = float(os.environ.get("SR_SL_BUFFER_PTS", "3.0"))
BASE_ZONE_TOLERANCE: Final[float] = float(os.environ.get("SR_ZONE_TOLERANCE_PTS", "20"))
PRIOR_SWING_LOOKBACK_DAYS: Final[int] = int(os.environ.get("SR_PRIOR_SWING_LOOKBACK_DAYS", "30"))

SESSION_START: Final[dtime] = parse_ist_time("SR_SESSION_START_IST", 9, 15)
OR_END: Final[dtime] = parse_ist_time("SR_OR_END_IST", 9, 30)
ENTRY_START: Final[dtime] = parse_ist_time("SR_ENTRY_START_IST", 9, 35)
NO_ENTRY_AFTER: Final[dtime] = parse_ist_time("SR_NO_ENTRY_AFTER_IST", 14, 45)
SQUARE_OFF_TIME: Final[dtime] = parse_ist_time("SR_SQUARE_OFF_IST", 14, 55)
SESSION_END: Final[dtime] = parse_ist_time("SR_SESSION_END_IST", 15, 30)


@dataclass(frozen=True)
class SRZone:
    level: float
    kind: str  # SUPPORT | RESISTANCE
    label: str


@dataclass
class SRPosition:
    direction: str
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    opened_at: str
    entry_reason: str
    instrument_key: str = ""
    option_strike: int = 0
    option_type: str = ""
    lot_size: int = 65
    quantity: int = 75


@dataclass
class SRState:
    config: IndexConfig
    trade_day: str = ""
    spot: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    or_ready: bool = False
    session_high: float | None = None
    session_low: float | None = None
    prior_swing_high: float | None = None
    prior_swing_low: float | None = None
    prior_swing_high_at: str = ""
    prior_swing_low_at: str = ""
    prior_swings_loaded: bool = False
    swing_high: float | None = None
    swing_low: float | None = None
    active_zones: list[dict[str, Any]] = field(default_factory=list)
    setup_label: str = "Intraday S/R — building opening range"
    position: SRPosition | None = None
    trades_today: int = 0
    last_candle_ts: str = ""
    signal_log: list[str] = field(default_factory=list)


def zone_tolerance(cfg: IndexConfig) -> float:
    return BASE_ZONE_TOLERANCE * (cfg.strike_step / 50.0)


def _parse_candle_ts(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw)
    return ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)


def aggregate_session_hourly(candles: list[dict[str, float]], day: date) -> list[dict[str, float]]:
    buckets: dict[datetime, list[dict[str, float]]] = {}
    for c in candles:
        ts = _parse_candle_ts(c["timestamp"])
        if ts.date() != day or ts.time() < SESSION_START:
            continue
        hour_key = ts.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour_key, []).append(c)
    out: list[dict[str, float]] = []
    for hour in sorted(buckets):
        bars = buckets[hour]
        out.append(
            {
                "timestamp": hour.isoformat(),
                "open": float(bars[0]["open"]),
                "high": max(float(b["high"]) for b in bars),
                "low": min(float(b["low"]) for b in bars),
                "close": float(bars[-1]["close"]),
                "volume": sum(float(b.get("volume") or 0) for b in bars),
            }
        )
    return out


def closed_hourly_bars(hourly: list[dict[str, float]], now: datetime) -> list[dict[str, float]]:
    closed: list[dict[str, float]] = []
    for bar in hourly:
        ts = _parse_candle_ts(bar["timestamp"])
        if ts + timedelta(hours=1) <= now:
            closed.append(bar)
    return closed


def last_swing_high_low(hourly_closed: list[dict[str, float]]) -> tuple[float | None, float | None]:
    sh, sl, _, _ = last_swing_high_low_detailed(hourly_closed)
    return sh, sl


def last_swing_high_low_detailed(
    hourly_closed: list[dict[str, float]],
) -> tuple[float | None, float | None, str, str]:
    """Most recent confirmed 1H pivot high and pivot low in the series."""
    if len(hourly_closed) < 3:
        return None, None, "", ""
    swing_h: float | None = None
    swing_l: float | None = None
    swing_h_at = ""
    swing_l_at = ""
    for i in range(1, len(hourly_closed) - 1):
        h = float(hourly_closed[i]["high"])
        l = float(hourly_closed[i]["low"])
        if h > float(hourly_closed[i - 1]["high"]) and h > float(hourly_closed[i + 1]["high"]):
            swing_h = h
            swing_h_at = str(hourly_closed[i]["timestamp"])
        if l < float(hourly_closed[i - 1]["low"]) and l < float(hourly_closed[i + 1]["low"]):
            swing_l = l
            swing_l_at = str(hourly_closed[i]["timestamp"])
    return swing_h, swing_l, swing_h_at, swing_l_at


def parse_historical_candles(data: Any) -> list[dict[str, float]]:
    if not isinstance(data, dict):
        return []
    rows = data.get("candles")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, float]] = []
    for row in rows:
        candle = _parse_v3_candle_row(row)
        if candle is not None:
            out.append(candle)
    out.sort(key=lambda c: c["timestamp"])
    return out


def closed_hourly_before(hourly: list[dict[str, float]], before: datetime) -> list[dict[str, float]]:
    closed: list[dict[str, float]] = []
    for bar in hourly:
        ts = _parse_candle_ts(bar["timestamp"])
        if ts + timedelta(hours=1) <= before:
            closed.append(bar)
    return closed


def prior_1h_swings_before_open(
    hourly: list[dict[str, float]],
    session_open: datetime,
) -> tuple[float | None, float | None, str, str]:
    """Last confirmed 1H swing H/L strictly before today's session open."""
    closed = closed_hourly_before(hourly, session_open)
    return last_swing_high_low_detailed(closed)


def _level_near(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance


def build_intraday_zones(state: SRState, tolerance: float = 0.0) -> list[SRZone]:
    zones: list[SRZone] = []
    if state.prior_swing_high is not None:
        zones.append(SRZone(state.prior_swing_high, "RESISTANCE", "Prior 1H Swing High"))
    if state.prior_swing_low is not None:
        zones.append(SRZone(state.prior_swing_low, "SUPPORT", "Prior 1H Swing Low"))
    if state.swing_high is not None and (
        state.prior_swing_high is None or not _level_near(state.swing_high, state.prior_swing_high, tolerance)
    ):
        zones.append(SRZone(state.swing_high, "RESISTANCE", "Today 1H Swing High"))
    if state.swing_low is not None and (
        state.prior_swing_low is None or not _level_near(state.swing_low, state.prior_swing_low, tolerance)
    ):
        zones.append(SRZone(state.swing_low, "SUPPORT", "Today 1H Swing Low"))
    if state.or_high is not None:
        zones.append(SRZone(state.or_high, "RESISTANCE", "OR High"))
    if state.or_low is not None:
        zones.append(SRZone(state.or_low, "SUPPORT", "OR Low"))
    if state.session_high is not None:
        zones.append(SRZone(state.session_high, "RESISTANCE", "Session High"))
    if state.session_low is not None:
        zones.append(SRZone(state.session_low, "SUPPORT", "Session Low"))
    return zones


def detect_sr_reversal(
    candles: list[dict[str, float]],
    zones: list[SRZone],
    tolerance: float,
) -> tuple[str | None, float, str]:
    """Return (direction, sl_anchor, reason) on last closed 5m bar."""
    if len(candles) < 3 or not zones:
        return None, 0.0, ""
    c = candles[-1]
    o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
    rng = max(h - l, 0.05)
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l
    vols = [float(x.get("volume") or 0) for x in candles[-21:-1]]
    avg_vol = sum(vols) / len(vols) if vols and sum(vols) > 0 else float(c.get("volume") or 1)
    vol_ok = float(c.get("volume") or 0) >= avg_vol * VOL_SPIKE

    best: tuple[str, float, str, float] | None = None

    for zone in zones:
        level = zone.level
        if zone.kind == "RESISTANCE":
            touched = h >= level - tolerance
            rejected = cl < level and upper_wick / rng >= WICK_RATIO
            if touched and rejected and (vol_ok or upper_wick / rng >= WICK_RATIO + 0.08):
                dist = abs(cl - level)
                if best is None or dist < best[3]:
                    sl = h + SL_BUFFER
                    reason = f"Rejection @ {zone.label} ({level:.2f})"
                    best = ("SHORT", sl, reason, dist)
        else:
            touched = l <= level + tolerance
            rejected = cl > level and lower_wick / rng >= WICK_RATIO
            if touched and rejected and (vol_ok or lower_wick / rng >= WICK_RATIO + 0.08):
                dist = abs(cl - level)
                if best is None or dist < best[3]:
                    sl = l - SL_BUFFER
                    reason = f"Bounce @ {zone.label} ({level:.2f})"
                    best = ("LONG", sl, reason, dist)

    if best is None:
        return None, 0.0, ""
    return best[0], best[1], best[2]


def refresh_intraday_levels(state: SRState, candles: list[dict[str, float]], now: datetime) -> None:
    day = now.date()
    session_candles = []
    or_candles = []
    for c in candles:
        ts = _parse_candle_ts(c["timestamp"])
        if ts.date() != day:
            continue
        if ts.time() >= SESSION_START:
            session_candles.append(c)
        if SESSION_START <= ts.time() < OR_END:
            or_candles.append(c)

    if or_candles:
        state.or_high = max(float(c["high"]) for c in or_candles)
        state.or_low = min(float(c["low"]) for c in or_candles)
        state.or_ready = True
    elif now.time() >= OR_END and session_candles:
        early = session_candles[:3]
        state.or_high = max(float(c["high"]) for c in early)
        state.or_low = min(float(c["low"]) for c in early)
        state.or_ready = True

    if session_candles:
        state.session_high = max(float(c["high"]) for c in session_candles)
        state.session_low = min(float(c["low"]) for c in session_candles)

    hourly = aggregate_session_hourly(candles, day)
    closed = closed_hourly_bars(hourly, now)
    state.swing_high, state.swing_low = last_swing_high_low(closed)

    tol = zone_tolerance(state.config)
    zones = build_intraday_zones(state, tol)
    state.active_zones = [{"level": z.level, "kind": z.kind, "label": z.label} for z in zones]

    if not state.prior_swings_loaded:
        state.setup_label = "Loading prior 1H swing levels…"
    elif state.position is None:
        parts = []
        if state.prior_swing_high:
            parts.append(f"prior H {state.prior_swing_high:.2f}")
        if state.prior_swing_low:
            parts.append(f"prior L {state.prior_swing_low:.2f}")
        if state.swing_high:
            parts.append(f"today H {state.swing_high:.2f}")
        if state.swing_low:
            parts.append(f"today L {state.swing_low:.2f}")
        state.setup_label = " · ".join(parts) or "Watching S/R zones"


class SRMarketClient:
    def __init__(self) -> None:
        self._upstox: UpstoxClient | None = None if MOCK_MODE else build_upstox_client()
        self._mock: dict[str, float] = {"NIFTY": 23_100.0, "BANKNIFTY": 51_200.0, "SENSEX": 76_400.0}

    def refresh_token(self) -> None:
        if self._upstox:
            self._upstox.refresh_access_token_from_disk()

    def get_spot(self, cfg: IndexConfig) -> float | None:
        if MOCK_MODE:
            base = self._mock.get(cfg.code, 23_100.0)
            val = round(base + random.uniform(-5, 5), 2)
            self._mock[cfg.code] = val
            return val
        if self._upstox:
            ltp = self._upstox.get_ltp(cfg.spot_instrument_key)
            if ltp is not None:
                return ltp
        return None

    def get_candles(self, cfg: IndexConfig) -> list[dict[str, float]] | None:
        if MOCK_MODE:
            now = datetime.now(IST)
            spot = self._mock.get(cfg.code, 23_100.0)
            out = []
            for i in range(12, 0, -1):
                ts = now - timedelta(minutes=CANDLE_5M * i)
                c = spot + random.uniform(-25, 25)
                out.append(
                    {
                        "timestamp": ts.isoformat(),
                        "open": c - 5,
                        "high": c + 12,
                        "low": c - 14,
                        "close": c,
                        "volume": 90_000 + random.randint(0, 50_000),
                    }
                )
            return out
        if not self._upstox:
            return []
        key = quote(cfg.spot_instrument_key, safe="")
        v3 = self._upstox.base_url.replace("/v2", "/v3")
        data = self._upstox._get(f"{v3}/historical-candle/intraday/{key}/minutes/{CANDLE_5M}")  # noqa: SLF001
        return parse_v3_intraday_candles(data, datetime.now(IST))

    def get_historical_1h_candles(self, cfg: IndexConfig) -> list[dict[str, float]]:
        if MOCK_MODE:
            return self._mock_historical_1h(cfg)
        if not self._upstox:
            return []
        today = datetime.now(IST).date()
        to_date = today - timedelta(days=1)
        from_date = today - timedelta(days=PRIOR_SWING_LOOKBACK_DAYS)
        key = quote(cfg.spot_instrument_key, safe="")
        v3 = self._upstox.base_url.replace("/v2", "/v3")
        url = f"{v3}/historical-candle/{key}/hours/1/{to_date.isoformat()}/{from_date.isoformat()}"
        data = self._upstox._get(url)  # noqa: SLF001
        candles = parse_historical_candles(data)
        if candles:
            return candles
        v2_url = (
            f"{self._upstox.base_url}/historical-candle/{key}/hour/"
            f"{to_date.isoformat()}/{from_date.isoformat()}"
        )
        data = self._upstox._get(v2_url)  # noqa: SLF001
        return parse_historical_candles(data)

    def get_prior_1h_swings(
        self,
        cfg: IndexConfig,
        session_open: datetime,
    ) -> tuple[float | None, float | None, str, str]:
        hourly = self.get_historical_1h_candles(cfg)
        if not hourly:
            logger.warning("[%s] no 1H history for prior swing lookup", cfg.code)
            return None, None, "", ""
        sh, sl, sh_at, sl_at = prior_1h_swings_before_open(hourly, session_open)
        if sh is None and sl is None:
            logger.warning("[%s] could not resolve prior 1H swings before %s", cfg.code, session_open.date())
        else:
            logger.info(
                "[%s] prior 1H swings before open: H=%s (%s) L=%s (%s)",
                cfg.code,
                sh,
                sh_at[:16] if sh_at else "—",
                sl,
                sl_at[:16] if sl_at else "—",
            )
        return sh, sl, sh_at, sl_at

    def _mock_historical_1h(self, cfg: IndexConfig) -> list[dict[str, float]]:
        """Synthetic hourly series with clear pivots for mock cockpit."""
        spot = self._mock.get(cfg.code, 23_100.0)
        now = datetime.now(IST)
        session_open = datetime.combine(now.date(), SESSION_START, tzinfo=IST)
        out: list[dict[str, float]] = []
        price = spot
        for i in range(40, 0, -1):
            ts = session_open - timedelta(hours=i)
            drift = random.uniform(-80, 80)
            if i in (12, 8, 4):
                drift = abs(drift) + 120
            if i in (16, 10, 6):
                drift = -abs(drift) - 120
            c = price + drift
            out.append(
                {
                    "timestamp": ts.isoformat(),
                    "open": c - 20,
                    "high": c + 35,
                    "low": c - 35,
                    "close": c,
                    "volume": 100_000,
                }
            )
            price = c
        return out

    def resolve_option(self, cfg: IndexConfig, spot: float, direction: str) -> dict[str, Any] | None:
        if self._upstox and not MOCK_MODE:
            c = self._upstox.get_itm_option_contract(cfg.spot_instrument_key, spot, direction)
            if c:
                return c
        desired = spot - ITM_OFFSET_POINTS if direction == "LONG" else spot + ITM_OFFSET_POINTS
        strike = int(round(desired / cfg.strike_step) * cfg.strike_step)
        return {"instrument_key": "", "strike": strike, "option_type": "CE" if direction == "LONG" else "PE"}

    def place_entry(self, instrument_key: str, quantity: int) -> bool:
        if PAPER_TRADING or not instrument_key:
            return True
        return bool(self._upstox and self._upstox.place_market_order(instrument_key, quantity, "BUY"))

    def place_exit(self, instrument_key: str, quantity: int) -> bool:
        if PAPER_TRADING or not instrument_key:
            return True
        return bool(self._upstox and self._upstox.place_market_order(instrument_key, quantity, "SELL"))


class SRReversalEngine:
    def __init__(self) -> None:
        self.client = SRMarketClient()
        self.states = {code: SRState(config=cfg) for code, cfg in INDEX_CONFIGS.items()}
        logger.info(
            "S/R Reversal engine started (paper=%s mock=%s indices=%s intraday-only)",
            PAPER_TRADING,
            MOCK_MODE,
            list(INDEX_CONFIGS.keys()),
        )

    def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                logger.exception("SR tick failed: %s", exc)
            time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))

    def tick(self) -> None:
        now = datetime.now(IST)
        self.client.refresh_token()
        self._roll_trade_day(now)

        if now.time() >= SESSION_END:
            self._square_off_all("SESSION_END", now)
            self._publish_all(now, True)
            return

        if kill_switch_engaged():
            self._square_off_all("KILL_SWITCH", now)

        entries_blocked = kill_switch_engaged() or now.time() >= NO_ENTRY_AFTER
        if now.time() >= SQUARE_OFF_TIME:
            self._square_off_all("TIME_GATE_1455", now)
            entries_blocked = True

        for state in self.states.values():
            self._process(state, now, entries_blocked)
        self._publish_heartbeat(now)

    def _roll_trade_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        for state in self.states.values():
            if state.trade_day != today:
                state.trade_day = today
                state.trades_today = 0
                state.position = None
                state.or_high = state.or_low = None
                state.session_high = state.session_low = None
                state.swing_high = state.swing_low = None
                state.prior_swing_high = state.prior_swing_low = None
                state.prior_swing_high_at = state.prior_swing_low_at = ""
                state.prior_swings_loaded = False
                state.or_ready = False
                state.active_zones = []
                state.setup_label = "New session — loading prior 1H swings"
                state.signal_log = []
                session_open = datetime.combine(now.date(), SESSION_START, tzinfo=IST)
                sh, sl, sh_at, sl_at = self.client.get_prior_1h_swings(state.config, session_open)
                state.prior_swing_high = sh
                state.prior_swing_low = sl
                state.prior_swing_high_at = sh_at
                state.prior_swing_low_at = sl_at
                state.prior_swings_loaded = sh is not None or sl is not None

    def _process(self, state: SRState, now: datetime, entries_blocked: bool) -> None:
        spot = self.client.get_spot(state.config)
        if spot is not None:
            state.spot = spot
        candles = self.client.get_candles(state.config) or []
        refresh_intraday_levels(state, candles, now)

        if state.position:
            self._manage_position(state, now)
        elif (
            not entries_blocked
            and state.prior_swings_loaded
            and now.time() >= ENTRY_START
            and state.trades_today < MAX_TRADES_PER_DAY
            and candles
        ):
            self._seek_entry(state, candles, now)

        self._publish_state(state, now, entries_blocked)

    def _seek_entry(self, state: SRState, candles: list[dict[str, float]], now: datetime) -> None:
        candle = candles[-1]
        if candle["timestamp"] == state.last_candle_ts:
            return
        state.last_candle_ts = candle["timestamp"]

        zones = build_intraday_zones(state, zone_tolerance(state.config))
        tol = zone_tolerance(state.config)
        direction, sl, reason = detect_sr_reversal(candles, zones, tol)
        if direction is None:
            return

        entry = float(candle["close"])
        tp1, tp2, _ = rr_book_targets(entry, sl, direction)
        contract = self.client.resolve_option(state.config, entry, direction)
        if contract is None:
            return
        qty = state.config.lot_size * LOTS_PER_TRADE
        if not self.client.place_entry(str(contract.get("instrument_key") or ""), qty):
            return
        state.position = SRPosition(
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            opened_at=now.isoformat(),
            entry_reason=reason,
            instrument_key=str(contract.get("instrument_key") or ""),
            option_strike=int(contract["strike"]),
            option_type=str(contract["option_type"]),
            lot_size=state.config.lot_size,
            quantity=qty,
        )
        state.trades_today += 1
        msg = (
            f"{state.config.display} S/R {direction} @ {entry:.2f} "
            f"SL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f} (book @ TP1) — {reason}"
        )
        state.signal_log.append(msg)
        logger.info(msg)
        telegram_notifier.notify_trade_execution(
            index_name=f"{state.config.display} S/R ({contract['strike']}{contract['option_type']})",
            trade_type=direction,
            entry_price=entry,
            target_price=tp1,
            sl_price=sl,
            tp2_price=tp2,
            component_sentiment=reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _manage_position(self, state: SRState, now: datetime) -> None:
        pos = state.position
        if pos is None:
            return
        spot = state.spot if state.spot is not None else pos.entry_price
        if now.time() >= SQUARE_OFF_TIME:
            self._close_position(state, pos, spot, "INTRADAY_SQUARE_OFF_1455", now)
            return
        reason = ""
        if pos.direction == "LONG":
            if spot <= pos.sl_price:
                reason = "SL hit"
            elif spot >= pos.tp1_price:
                reason = "TP1 booked (1R)"
        else:
            if spot >= pos.sl_price:
                reason = "SL hit"
            elif spot <= pos.tp1_price:
                reason = "TP1 booked (1R)"
        if reason:
            self._close_position(state, pos, spot, reason, now)

    def _close_position(self, state: SRState, pos: SRPosition, spot: float, reason: str, now: datetime) -> None:
        if pos.instrument_key:
            if not self.client.place_exit(pos.instrument_key, pos.quantity):
                state.setup_label = f"Exit pending — {reason}"
                return
        pnl = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_SR_REVERSAL,
            strategy_id="sr_reversal",
            symbol=state.config.code,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=spot,
            pnl_points=pnl,
            exit_reason=reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
        )
        state.position = None
        state.setup_label = f"Flat — {reason}"
        state.signal_log.append(f"Exit {reason} @ {spot:.2f}")
        telegram_notifier.notify_trade_exit(
            index_name=f"{state.config.display} S/R ({pos.option_strike}{pos.option_type})",
            trade_type=pos.direction,
            exit_price=spot,
            pnl_points=pnl,
            reason=reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _square_off_all(self, reason: str, now: datetime) -> None:
        for state in self.states.values():
            if state.position:
                spot = state.spot if state.spot is not None else state.position.entry_price
                self._close_position(state, state.position, spot, reason, now)

    def _publish_state(self, state: SRState, now: datetime, entries_blocked: bool) -> None:
        payload: dict[str, Any] = {
            "index": state.config.code,
            "display": state.config.display,
            "strategy": "S/R Reversal",
            "spot": state.spot,
            "or_high": state.or_high,
            "or_low": state.or_low,
            "or_ready": state.or_ready,
            "session_high": state.session_high,
            "session_low": state.session_low,
            "prior_swing_high": state.prior_swing_high,
            "prior_swing_low": state.prior_swing_low,
            "prior_swing_high_at": state.prior_swing_high_at,
            "prior_swing_low_at": state.prior_swing_low_at,
            "prior_swings_loaded": state.prior_swings_loaded,
            "swing_high": state.swing_high,
            "swing_low": state.swing_low,
            "zones": state.active_zones,
            "setup_label": state.setup_label,
            "trades_today": state.trades_today,
            "max_trades": MAX_TRADES_PER_DAY,
            "entries_blocked": entries_blocked,
            "paper_trading": PAPER_TRADING,
            "intraday_only": True,
            "signals": state.signal_log[-8:],
            "updated_at": now.isoformat(),
        }
        pos = state.position
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
            }
        cache_manager.set_json(
            cache_manager.SR_REVERSAL_STATE_KEY_TEMPLATE.format(index=state.config.code),
            payload,
            ttl_seconds=120,
        )

    def _publish_heartbeat(self, now: datetime) -> None:
        cache_manager.set_json(
            cache_manager.SR_REVERSAL_HEARTBEAT_KEY,
            {
                "at": now.isoformat(),
                "paper_trading": PAPER_TRADING,
                "mock": MOCK_MODE,
                "session_end_ist": SESSION_END.strftime("%H:%M"),
                "intraday_only": True,
                "instruments": list(INDEX_CONFIGS.keys()),
            },
            ttl_seconds=60,
        )

    def _publish_all(self, now: datetime, blocked: bool) -> None:
        for state in self.states.values():
            self._publish_state(state, now, blocked)
        self._publish_heartbeat(now)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    SRReversalEngine().run()


if __name__ == "__main__":
    main()
