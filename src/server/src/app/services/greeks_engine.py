"""Strategy Type 5 — Advanced Greeks (intraday).

Dealer-positioning read from the weekly option chain:
  - IV skew (put vs call)
  - OI-weighted net delta exposure
  - Gamma flip level (GEX zero-cross proxy)
  - Regime filter (positive vs negative gamma)

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
from app.services.engine_intraday import kill_switch_engaged, parse_ist_time, rr_book_targets, session_vwap
from app.services.options_greeks import ChainAnalytics, analyze_option_chain
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

logger = logging.getLogger("ak07.greeks_engine")

IST: Final = ZoneInfo("Asia/Kolkata")
POLL_SECONDS: Final[float] = float(os.environ.get("GREEKS_POLL_SECONDS", "15"))
CANDLE_5M: Final[int] = 5
LOTS_PER_TRADE: Final[int] = 1
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("GREEKS_MAX_TRADES_PER_DAY", "2"))
SKEW_ENTRY: Final[float] = float(os.environ.get("GREEKS_SKEW_ENTRY_PCT", "1.0"))
SL_BUFFER: Final[float] = 2.0

SESSION_START: Final[dtime] = parse_ist_time("GREEKS_SESSION_START_IST", 9, 15)
ENTRY_START: Final[dtime] = parse_ist_time("GREEKS_ENTRY_START_IST", 9, 25)
NO_ENTRY_AFTER: Final[dtime] = parse_ist_time("GREEKS_NO_ENTRY_AFTER_IST", 14, 45)
SQUARE_OFF_TIME: Final[dtime] = parse_ist_time("GREEKS_SQUARE_OFF_IST", 14, 55)
SESSION_END: Final[dtime] = parse_ist_time("GREEKS_SESSION_END_IST", 15, 30)


@dataclass
class GreeksPosition:
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
    lot_size: int = 75
    quantity: int = 75


@dataclass
class GreeksState:
    config: IndexConfig
    trade_day: str = ""
    spot: float | None = None
    chain: ChainAnalytics | None = None
    session_vwap: float | None = None
    setup_label: str = "Waiting for chain analytics"
    position: GreeksPosition | None = None
    trades_today: int = 0
    last_signal_bar: str = ""
    signal_log: list[str] = field(default_factory=list)


def greeks_entry_signal(
    analytics: ChainAnalytics,
    *,
    spot: float,
    vwap: float | None,
    candle_close: float,
    candle_open: float,
) -> tuple[str | None, float, str]:
    """Institutional greeks + momentum confluence."""
    flip = analytics.gamma_flip
    if flip is None:
        return None, 0.0, ""

    bullish_momentum = candle_close > candle_open and (vwap is None or candle_close > vwap)
    bearish_momentum = candle_close < candle_open and (vwap is None or candle_close < vwap)

    # Fade rich put skew above gamma flip (dealers hedging short puts → squeeze)
    if (
        spot > flip
        and analytics.skew_pct >= SKEW_ENTRY
        and analytics.bias == "BULLISH"
        and bullish_momentum
    ):
        sl = min(flip, spot) - SL_BUFFER - 5
        return "LONG", sl, f"Above gamma flip {flip:.0f} · skew {analytics.skew_pct:+.1f}% · {analytics.regime}"

    # Rich call skew below flip — breakdown continuation
    if (
        spot < flip
        and analytics.skew_pct <= -SKEW_ENTRY
        and analytics.bias == "BEARISH"
        and bearish_momentum
    ):
        sl = max(flip, spot) + SL_BUFFER + 5
        return "SHORT", sl, f"Below gamma flip {flip:.0f} · skew {analytics.skew_pct:+.1f}% · {analytics.regime}"

    # Negative GEX — momentum follows break of flip
    if analytics.regime == "NEGATIVE_GEX" and spot > flip and bullish_momentum:
        return "LONG", flip - SL_BUFFER, "Negative GEX momentum long through flip"
    if analytics.regime == "NEGATIVE_GEX" and spot < flip and bearish_momentum:
        return "SHORT", flip + SL_BUFFER, "Negative GEX momentum short through flip"

    return None, 0.0, ""


class GreeksMarketClient:
    def __init__(self) -> None:
        self._upstox: UpstoxClient | None = None if MOCK_MODE else build_upstox_client()
        self._mock: dict[str, float] = {
            "NIFTY": 23_100.0,
            "BANKNIFTY": 51_200.0,
            "SENSEX": 76_400.0,
        }

    def refresh_token(self) -> None:
        if self._upstox:
            self._upstox.refresh_access_token_from_disk()

    def get_spot(self, cfg: IndexConfig) -> float | None:
        if MOCK_MODE:
            base = self._mock.get(cfg.code, 23_100.0)
            val = round(base + random.uniform(-8, 8), 2)
            self._mock[cfg.code] = val
            return val
        if self._upstox:
            return self._upstox.get_ltp(cfg.spot_instrument_key)
        return None

    def get_candles(self, cfg: IndexConfig) -> list[dict[str, float]] | None:
        if MOCK_MODE:
            now = datetime.now(IST)
            c = self._mock.get(cfg.code, 23_100.0)
            return [
                {
                    "timestamp": (now - timedelta(minutes=CANDLE_5M)).isoformat(),
                    "open": c - 4,
                    "high": c + 6,
                    "low": c - 7,
                    "close": c + 2,
                    "volume": 120_000,
                }
            ]
        if not self._upstox:
            return []
        key = quote(cfg.spot_instrument_key, safe="")
        v3 = self._upstox.base_url.replace("/v2", "/v3")
        data = self._upstox._get(f"{v3}/historical-candle/intraday/{key}/minutes/{CANDLE_5M}")  # noqa: SLF001
        return parse_v3_intraday_candles(data, datetime.now(IST))

    def get_chain_analytics(self, cfg: IndexConfig, spot: float) -> ChainAnalytics | None:
        if MOCK_MODE:
            from datetime import date as dt_date

            expiry = (dt_date.today() + timedelta(days=3)).isoformat()
            step = cfg.strike_step
            fake_rows = []
            for i in range(-8, 9):
                strike = int(round((spot + i * step) / step) * step)
                fake_rows.append(
                    {
                        "strike_price": strike,
                        "call_options": {
                            "market_data": {"oi": 1_000_000 + i * 50_000, "ltp": max(spot - strike + 80, 5)}
                        },
                        "put_options": {
                            "market_data": {"oi": 1_200_000 - i * 40_000, "ltp": max(strike - spot + 90, 5)}
                        },
                    }
                )
            return analyze_option_chain(spot=spot, chain_rows=fake_rows, expiry=expiry)
        if not self._upstox:
            return None
        expiry, rows = self._upstox.get_option_chain_with_expiry(cfg.spot_instrument_key)
        if not expiry or not rows:
            return None
        return analyze_option_chain(spot=spot, chain_rows=rows, expiry=expiry)

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


class GreeksEngine:
    def __init__(self) -> None:
        self.client = GreeksMarketClient()
        self.states = {code: GreeksState(config=cfg) for code, cfg in INDEX_CONFIGS.items()}
        logger.info(
            "Greeks engine started (paper=%s mock=%s indices=%s)",
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
                logger.exception("Greeks tick failed: %s", exc)
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
                state.chain = None
                state.signal_log = []
                state.setup_label = "New session — loading greeks"

    def _process(self, state: GreeksState, now: datetime, entries_blocked: bool) -> None:
        cfg = state.config
        spot = self.client.get_spot(cfg)
        if spot is not None:
            state.spot = spot
        candles = self.client.get_candles(cfg) or []
        state.session_vwap = session_vwap(candles) if candles else None
        if spot is not None:
            state.chain = self.client.get_chain_analytics(cfg, spot)

        chain = state.chain
        if chain:
            flip = f"{chain.gamma_flip:.0f}" if chain.gamma_flip else "—"
            state.setup_label = (
                f"Flip {flip} · skew {chain.skew_pct:+.1f}% · PCR {chain.pcr_oi:.2f} · "
                f"{chain.regime} · bias {chain.bias}"
            )

        if state.position:
            self._manage_position(state, now)
        elif (
            not entries_blocked
            and chain
            and spot is not None
            and now.time() >= ENTRY_START
            and now.time() >= SESSION_START
            and state.trades_today < MAX_TRADES_PER_DAY
            and candles
        ):
            self._seek_entry(state, candles, now)

        self._publish_state(state, now, entries_blocked)

    def _seek_entry(self, state: GreeksState, candles: list[dict[str, float]], now: datetime) -> None:
        chain = state.chain
        spot = state.spot
        if chain is None or spot is None:
            return
        bar = candles[-1]
        if bar["timestamp"] == state.last_signal_bar:
            return
        state.last_signal_bar = bar["timestamp"]
        direction, sl, reason = greeks_entry_signal(
            chain,
            spot=spot,
            vwap=state.session_vwap,
            candle_close=float(bar["close"]),
            candle_open=float(bar["open"]),
        )
        if direction is None:
            return
        entry = float(bar["close"])
        tp1, tp2, _ = rr_book_targets(entry, sl, direction)
        contract = self.client.resolve_option(state.config, entry, direction)
        if contract is None:
            return
        qty = state.config.lot_size * LOTS_PER_TRADE
        if not self.client.place_entry(str(contract.get("instrument_key") or ""), qty):
            return
        state.position = GreeksPosition(
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
            f"{state.config.display} Greeks {direction} @ {entry:.2f} "
            f"SL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f} (book @ TP1) — {reason}"
        )
        state.signal_log.append(msg)
        logger.info(msg)
        telegram_notifier.notify_trade_execution(
            index_name=f"{state.config.display} Greeks ({contract['strike']}{contract['option_type']})",
            trade_type=direction,
            entry_price=entry,
            target_price=tp1,
            sl_price=sl,
            tp2_price=tp2,
            component_sentiment=chain.bias,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _manage_position(self, state: GreeksState, now: datetime) -> None:
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

    def _close_position(
        self, state: GreeksState, pos: GreeksPosition, spot: float, reason: str, now: datetime
    ) -> None:
        if pos.instrument_key and not self.client.place_exit(pos.instrument_key, pos.quantity):
            state.setup_label = f"Exit pending — {reason}"
            return
        pnl = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_GREEKS,
            strategy_id="greeks",
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
            index_name=f"{state.config.display} Greeks ({pos.option_strike}{pos.option_type})",
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

    def _publish_state(self, state: GreeksState, now: datetime, entries_blocked: bool) -> None:
        chain = state.chain
        payload: dict[str, Any] = {
            "index": state.config.code,
            "display": state.config.display,
            "strategy": "Greeks",
            "spot": state.spot,
            "setup_label": state.setup_label,
            "trades_today": state.trades_today,
            "max_trades": MAX_TRADES_PER_DAY,
            "entries_blocked": entries_blocked,
            "paper_trading": PAPER_TRADING,
            "session_vwap": state.session_vwap,
            "signals": state.signal_log[-8:],
            "updated_at": now.isoformat(),
        }
        if chain:
            payload["analytics"] = {
                "atm_strike": chain.atm_strike,
                "atm_iv": chain.atm_iv,
                "skew_pct": chain.skew_pct,
                "pcr_oi": chain.pcr_oi,
                "net_delta_oi": chain.net_delta_oi,
                "net_gex": chain.net_gex,
                "gamma_flip": chain.gamma_flip,
                "regime": chain.regime,
                "bias": chain.bias,
                "expiry": chain.expiry,
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
            cache_manager.GREEKS_STATE_KEY_TEMPLATE.format(index=state.config.code),
            payload,
            ttl_seconds=120,
        )

    def _publish_heartbeat(self, now: datetime) -> None:
        cache_manager.set_json(
            cache_manager.GREEKS_HEARTBEAT_KEY,
            {
                "at": now.isoformat(),
                "paper_trading": PAPER_TRADING,
                "mock": MOCK_MODE,
                "session_end_ist": SESSION_END.strftime("%H:%M"),
                "indices": list(INDEX_CONFIGS.keys()),
            },
            ttl_seconds=60,
        )

    def _publish_all(self, now: datetime, entries_blocked: bool) -> None:
        for state in self.states.values():
            self._publish_state(state, now, entries_blocked)
        self._publish_heartbeat(now)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    GreeksEngine().run()


if __name__ == "__main__":
    main()
