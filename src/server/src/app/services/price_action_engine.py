"""Strategy Type 4 — Advanced Price Action (intraday).

Institutional PA stack on 5-minute structure:
  - Opening range (09:15–09:30 IST)
  - Session VWAP filter
  - Liquidity sweep + reclaim (stop hunt)
  - Break-of-structure confirmation + volume expansion

Nifty · BankNifty · Sensex · 1 lot ITM options · book @ TP1 (1R) · intraday flat 14:55 IST.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager, performance_store, telegram_notifier
from app.services.engine_intraday import direction_allowed_by_blr_day, kill_switch_engaged, parse_ist_time, rr_book_targets, session_vwap
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

logger = logging.getLogger("ak07.price_action_engine")

IST: Final = ZoneInfo("Asia/Kolkata")
POLL_SECONDS: Final[float] = float(os.environ.get("PA_POLL_SECONDS", "15"))
CANDLE_5M: Final[int] = 5
LOTS_PER_TRADE: Final[int] = 1
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("PA_MAX_TRADES_PER_DAY", "2"))
WICK_RATIO: Final[float] = 0.42
VOL_SPIKE: Final[float] = 1.15
SL_BUFFER: Final[float] = float(os.environ.get("PA_SL_BUFFER_PTS", "3.0"))

SESSION_START: Final[dtime] = parse_ist_time("PA_SESSION_START_IST", 9, 15)
OR_END: Final[dtime] = parse_ist_time("PA_OR_END_IST", 9, 30)
ENTRY_START: Final[dtime] = parse_ist_time("PA_ENTRY_START_IST", 9, 35)
NO_ENTRY_AFTER: Final[dtime] = parse_ist_time("PA_NO_ENTRY_AFTER_IST", 14, 45)
SQUARE_OFF_TIME: Final[dtime] = parse_ist_time("PA_SQUARE_OFF_IST", 14, 55)
SESSION_END: Final[dtime] = parse_ist_time("PA_SESSION_END_IST", 15, 30)


@dataclass
class PAPosition:
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
class PAState:
    config: IndexConfig
    trade_day: str = ""
    spot: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    or_ready: bool = False
    session_vwap: float | None = None
    swing_high: float | None = None
    swing_low: float | None = None
    structure: str = "NEUTRAL"
    setup_label: str = "Building opening range"
    position: PAPosition | None = None
    trades_today: int = 0
    last_candle_ts: str = ""
    signal_log: list[str] = field(default_factory=list)


def detect_pa_signal(
    candles: list[dict[str, float]],
    *,
    or_high: float,
    or_low: float,
    vwap: float | None,
    structure: str,
) -> tuple[str | None, float, str]:
    """Return (direction, sl_anchor, reason) on last closed 5m bar."""
    if len(candles) < 6:
        return None, 0.0, ""
    c = candles[-1]
    o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
    rng = max(h - l, 0.05)
    vols = [float(x.get("volume") or 0) for x in candles[-21:-1]]
    avg_vol = sum(vols) / len(vols) if vols and sum(vols) > 0 else float(c.get("volume") or 1)
    cur_vol = float(c.get("volume") or 0)
    vol_ok = cur_vol >= avg_vol * VOL_SPIKE

    lower_wick = min(o, cl) - l
    upper_wick = h - max(o, cl)

    # Liquidity sweep long: stab below OR low, close back inside + rejection wick
    if l < or_low - 2 and cl > or_low and lower_wick / rng >= WICK_RATIO:
        if vwap is None or cl > vwap:
            sl = l - SL_BUFFER
            return "LONG", sl, "OR sweep reclaim (bullish rejection)"

    # Liquidity sweep short
    if h > or_high + 2 and cl < or_high and upper_wick / rng >= WICK_RATIO:
        if vwap is None or cl < vwap:
            sl = h + SL_BUFFER
            return "SHORT", sl, "OR sweep reject (bearish rejection)"

    # BOS continuation
    if structure == "BULLISH" and cl > or_high and vol_ok and (vwap is None or cl > vwap):
        return "LONG", or_low - SL_BUFFER, "BOS above OR high + volume"
    if structure == "BEARISH" and cl < or_low and vol_ok and (vwap is None or cl < vwap):
        return "SHORT", or_high + SL_BUFFER, "BOS below OR low + volume"

    return None, 0.0, ""


def update_structure(candles: list[dict[str, float]], state: PAState) -> None:
    if len(candles) < 4:
        return
    highs = [float(c["high"]) for c in candles[-8:]]
    lows = [float(c["low"]) for c in candles[-8:]]
    state.swing_high = max(highs)
    state.swing_low = min(lows)
    mid = (highs[-1] + lows[-1]) / 2.0
    if highs[-1] > highs[-3] and lows[-1] > lows[-3]:
        state.structure = "BULLISH"
    elif highs[-1] < highs[-3] and lows[-1] < lows[-3]:
        state.structure = "BEARISH"
    elif state.spot is not None and state.session_vwap is not None:
        state.structure = "BULLISH" if state.spot > state.session_vwap else "BEARISH"
    else:
        state.structure = "NEUTRAL"
    _ = mid


class PAMarketClient:
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
            for i in range(8, 0, -1):
                ts = now - timedelta(minutes=CANDLE_5M * i)
                c = spot + random.uniform(-20, 20)
                out.append(
                    {
                        "timestamp": ts.isoformat(),
                        "open": c - 5,
                        "high": c + 10,
                        "low": c - 12,
                        "close": c,
                        "volume": 80_000 + random.randint(0, 40_000),
                    }
                )
            return out
        if not self._upstox:
            return []
        key = quote(cfg.spot_instrument_key, safe="")
        v3 = self._upstox.base_url.replace("/v2", "/v3")
        data = self._upstox._get(f"{v3}/historical-candle/intraday/{key}/minutes/{CANDLE_5M}")  # noqa: SLF001
        return parse_v3_intraday_candles(data, datetime.now(IST))

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


class PriceActionEngine:
    def __init__(self) -> None:
        self.client = PAMarketClient()
        self.states = {code: PAState(config=cfg) for code, cfg in INDEX_CONFIGS.items()}
        logger.info(
            "Price Action engine started (paper=%s mock=%s indices=%s)",
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
                logger.exception("PA tick failed: %s", exc)
            time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))

    def tick(self) -> None:
        now = datetime.now(IST)
        self.client.refresh_token()
        self._roll_trade_day(now)

        if now.time() >= SESSION_END:
            self._square_off_all("SESSION_END", now)
            self._publish_all(now, True, "session closed")
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
                state.or_ready = False
                state.setup_label = "New session — building opening range"
                state.signal_log = []

    def _process(self, state: PAState, now: datetime, entries_blocked: bool) -> None:
        spot = self.client.get_spot(state.config)
        if spot is not None:
            state.spot = spot
        candles = self.client.get_candles(state.config) or []
        state.session_vwap = session_vwap(candles) if candles else None
        self._refresh_opening_range(state, candles, now)
        if candles:
            update_structure(candles, state)

        if state.position:
            self._manage_position(state, now)
        elif (
            not entries_blocked
            and state.or_ready
            and now.time() >= ENTRY_START
            and state.trades_today < MAX_TRADES_PER_DAY
            and candles
        ):
            self._seek_entry(state, candles, now)

        self._publish_state(state, now, entries_blocked)

    def _refresh_opening_range(self, state: PAState, candles: list[dict[str, float]], now: datetime) -> None:
        if state.or_ready:
            return
        day = now.date()
        or_candles = []
        for c in candles:
            ts = datetime.fromisoformat(c["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.date() == day and SESSION_START <= ts.time() < OR_END:
                or_candles.append(c)
        if or_candles:
            state.or_high = max(float(c["high"]) for c in or_candles)
            state.or_low = min(float(c["low"]) for c in or_candles)
            state.or_ready = True
            state.setup_label = f"OR locked H={state.or_high:.2f} L={state.or_low:.2f} · {state.structure}"
            return
        if now.time() >= OR_END and candles:
            state.or_high = max(float(c["high"]) for c in candles[:3])
            state.or_low = min(float(c["low"]) for c in candles[:3])
            state.or_ready = True
            state.setup_label = f"OR approx H={state.or_high:.2f} L={state.or_low:.2f}"

    def _seek_entry(self, state: PAState, candles: list[dict[str, float]], now: datetime) -> None:
        if state.or_high is None or state.or_low is None:
            return
        candle = candles[-1]
        if candle["timestamp"] == state.last_candle_ts:
            return
        state.last_candle_ts = candle["timestamp"]
        direction, sl, reason = detect_pa_signal(
            candles,
            or_high=state.or_high,
            or_low=state.or_low,
            vwap=state.session_vwap,
            structure=state.structure,
        )
        if direction is None:
            state.setup_label = f"Watching PA — {state.structure} · VWAP {state.session_vwap or 0:.2f}"
            return
        allowed, blr_note = direction_allowed_by_blr_day(state.config.code, direction)
        if not allowed:
            state.setup_label = blr_note
            return
        entry = float(candle["close"])
        tp1, tp2, _ = rr_book_targets(entry, sl, direction)
        contract = self.client.resolve_option(state.config, entry, direction)
        if contract is None:
            return
        qty = state.config.lot_size * LOTS_PER_TRADE
        if not self.client.place_entry(str(contract.get("instrument_key") or ""), qty):
            return
        state.position = PAPosition(
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
            f"{state.config.display} PA {direction} @ {entry:.2f} "
            f"SL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f} (book @ TP1) — {reason}"
        )
        state.signal_log.append(msg)
        logger.info(msg)
        telegram_notifier.notify_trade_execution(
            index_name=f"{state.config.display} PA ({contract['strike']}{contract['option_type']})",
            trade_type=direction,
            entry_price=entry,
            target_price=tp1,
            sl_price=sl,
            tp2_price=tp2,
            component_sentiment=state.structure,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _manage_position(self, state: PAState, now: datetime) -> None:
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

    def _close_position(self, state: PAState, pos: PAPosition, spot: float, reason: str, now: datetime) -> None:
        if pos.instrument_key:
            if not self.client.place_exit(pos.instrument_key, pos.quantity):
                state.setup_label = f"Exit pending — {reason}"
                return
        pnl = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_PRICE_ACTION,
            strategy_id="price_action",
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
            index_name=f"{state.config.display} PA ({pos.option_strike}{pos.option_type})",
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

    def _publish_state(self, state: PAState, now: datetime, entries_blocked: bool) -> None:
        payload: dict[str, Any] = {
            "index": state.config.code,
            "display": state.config.display,
            "strategy": "Price Action",
            "spot": state.spot,
            "or_high": state.or_high,
            "or_low": state.or_low,
            "or_ready": state.or_ready,
            "session_vwap": state.session_vwap,
            "structure": state.structure,
            "setup_label": state.setup_label,
            "trades_today": state.trades_today,
            "max_trades": MAX_TRADES_PER_DAY,
            "entries_blocked": entries_blocked,
            "paper_trading": PAPER_TRADING,
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
            cache_manager.PA_STATE_KEY_TEMPLATE.format(index=state.config.code),
            payload,
            ttl_seconds=120,
        )

    def _publish_heartbeat(self, now: datetime) -> None:
        cache_manager.set_json(
            cache_manager.PA_HEARTBEAT_KEY,
            {
                "at": now.isoformat(),
                "paper_trading": PAPER_TRADING,
                "mock": MOCK_MODE,
                "session_end_ist": SESSION_END.strftime("%H:%M"),
                "instruments": list(INDEX_CONFIGS.keys()),
            },
            ttl_seconds=60,
        )

    def _publish_all(self, now: datetime, blocked: bool, reason: str) -> None:
        for state in self.states.values():
            self._publish_state(state, now, blocked)
        self._publish_heartbeat(now)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    PriceActionEngine().run()


if __name__ == "__main__":
    main()
