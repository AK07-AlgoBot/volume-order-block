"""SMC + CRT strategy engine (Strategy Type 2).

Candle Range Theory on the first 1H session candle (CRH / CRM / CRL), then
5-minute Fair Value Gap setups with 1:2 minimum R:R toward CRM and swing targets.
Nifty 50 only — live ITM options, 1 lot per trade.

Run: python -u src/server/src/app/services/smc_crt_engine.py
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
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager, telegram_notifier
from app.services import performance_store
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    ITM_OFFSET_POINTS,
    MOCK_MODE,
    PAPER_TRADING,
    UpstoxClient,
    parse_v3_intraday_candles,
)

logger = logging.getLogger("ak07.smc_crt_engine")

IST: Final = ZoneInfo("Asia/Kolkata")
POLL_SECONDS: Final[float] = float(os.environ.get("SMC_CRT_POLL_SECONDS", "15"))
CANDLE_5M: Final[int] = 5
CANDLE_1H: Final[int] = 60
MIN_RR: Final[float] = 2.0
CRM_BUFFER_POINTS: Final[float] = 8.0
LOTS_PER_TRADE: Final[int] = 1
MAX_TRADES_PER_DAY: Final[int] = 2


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


CRT_SESSION_START: Final[dtime] = _parse_ist_time("SMC_CRT_CRT_START_IST", 9, 15)
SESSION_END: Final[dtime] = _parse_ist_time("SMC_CRT_SESSION_END_IST", 15, 30)
SQUARE_OFF_TIME: Final[dtime] = _parse_ist_time("SMC_CRT_SQUARE_OFF_IST", 14, 55)


@dataclass(frozen=True)
class SMCCRTInstrument:
    code: str
    display: str
    spot_instrument_key: str
    crt_start: dtime
    baseline_spot: float


SMC_CRT_INSTRUMENTS: Final[dict[str, SMCCRTInstrument]] = {
    "NIFTY": SMCCRTInstrument(
        code="NIFTY",
        display="Nifty 50",
        spot_instrument_key="NSE_INDEX|Nifty 50",
        crt_start=CRT_SESSION_START,
        baseline_spot=23_100.0,
    ),
}


@dataclass
class FVGZone:
    direction: str  # LONG | SHORT
    low: float
    high: float
    candle_ts: str


@dataclass
class SMCCRTPosition:
    direction: str
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    opened_at: str
    fvg_low: float
    fvg_high: float
    instrument_key: str = ""
    option_strike: int = 0
    option_type: str = ""
    lot_size: int = 75
    quantity: int = 75


@dataclass
class InstrumentState:
    config: SMCCRTInstrument
    spot: float | None = None
    crh: float | None = None
    crm: float | None = None
    crl: float | None = None
    crt_ready: bool = False
    setup_label: str = "Waiting for 1H CRT"
    last_fvg: FVGZone | None = None
    swept_low: bool = False
    swept_high: bool = False
    position: SMCCRTPosition | None = None
    trades_today: int = 0
    last_signal_fvg_ts: str = ""
    signal_log: list[str] = field(default_factory=list)


def crt_from_1h_candle(candle: dict[str, float]) -> tuple[float, float, float]:
    high = float(candle["high"])
    low = float(candle["low"])
    return high, (high + low) / 2.0, low


def detect_bullish_fvg(candles: list[dict[str, float]]) -> FVGZone | None:
    if len(candles) < 3:
        return None
    c1, _, c3 = candles[-3], candles[-2], candles[-1]
    gap_low = float(c1["high"])
    gap_high = float(c3["low"])
    if gap_high > gap_low and float(c3["close"]) > float(c3["open"]):
        return FVGZone("LONG", gap_low, gap_high, str(c3["timestamp"]))
    return None


def detect_bearish_fvg(candles: list[dict[str, float]]) -> FVGZone | None:
    if len(candles) < 3:
        return None
    c1, _, c3 = candles[-3], candles[-2], candles[-1]
    gap_high = float(c1["low"])
    gap_low = float(c3["high"])
    if gap_low < gap_high and float(c3["close"]) < float(c3["open"]):
        return FVGZone("SHORT", gap_low, gap_high, str(c3["timestamp"]))
    return None


def near_crm(price: float, crm: float, band: float = CRM_BUFFER_POINTS) -> bool:
    return abs(price - crm) <= band


def rr_book_targets(entry: float, sl: float, direction: str) -> tuple[float, float, float]:
    """Return (tp1 at 1R, tp2 at 2R, risk points) for spot-based option trades."""
    risk = max(abs(entry - sl), 0.05)
    if direction == "LONG":
        return entry + risk, entry + 2.0 * risk, risk
    return entry - risk, entry - 2.0 * risk, risk


def rr_ok(entry: float, sl: float, target: float, direction: str) -> bool:
    risk = abs(entry - sl)
    reward = abs(target - entry) if direction == "LONG" else abs(entry - target)
    return risk > 0 and reward / risk >= MIN_RR


class SMCCRTMarketClient:
    """Upstox quotes + ITM option orders for Nifty SMC+CRT."""

    def __init__(self) -> None:
        self._upstox: UpstoxClient | None = None if MOCK_MODE else UpstoxClient()
        self._mock_spots: dict[str, float] = {
            code: cfg.baseline_spot for code, cfg in SMC_CRT_INSTRUMENTS.items()
        }
        self._mock_crt: dict[str, tuple[float, float, float]] = {}
        self._tick = 0

    def refresh_token(self) -> None:
        if self._upstox:
            self._upstox.refresh_access_token_from_disk()

    def instrument_key(self, cfg: SMCCRTInstrument) -> str:
        override = (os.environ.get(f"SMC_CRT_{cfg.code}_INSTRUMENT_KEY") or "").strip()
        return override or cfg.spot_instrument_key

    def get_spot(self, cfg: SMCCRTInstrument) -> float | None:
        if MOCK_MODE:
            return self._mock_spot(cfg)
        if self._upstox:
            key = self.instrument_key(cfg)
            ltp = self._upstox.get_ltp(key)
            if ltp is not None:
                self._mock_spots[cfg.code] = ltp
                return ltp
            logger.warning("Upstox LTP failed for %s (%s)", cfg.code, key)
        return self._mock_spot(cfg)

    def _mock_spot(self, cfg: SMCCRTInstrument) -> float:
        base = self._mock_spots.get(cfg.code, cfg.baseline_spot)
        drift = base * random.uniform(-0.0008, 0.0008)
        value = round(base + drift, 2)
        self._mock_spots[cfg.code] = value
        return value

    def get_candles(self, cfg: SMCCRTInstrument, minutes: int) -> list[dict[str, float]] | None:
        if MOCK_MODE:
            return self._mock_candles(cfg, minutes)
        if self._upstox:
            key = self.instrument_key(cfg)
            v3_base = self._upstox.base_url.replace("/v2", "/v3")
            url = f"{v3_base}/historical-candle/intraday/{key}/minutes/{minutes}"
            data = self._upstox._get(url)  # noqa: SLF001
            if data is not None:
                parsed = parse_v3_intraday_candles(data, datetime.now(IST))
                if parsed:
                    return parsed
            logger.warning("Upstox candles failed for %s (%s)", cfg.code, key)
        return self._mock_candles(cfg, minutes)

    def _mock_candles(self, cfg: SMCCRTInstrument, minutes: int) -> list[dict[str, float]]:
        now = datetime.now(IST)
        spot = self._mock_spots.get(cfg.code, cfg.baseline_spot)
        if cfg.code not in self._mock_crt and now.time() >= (
            datetime.combine(now.date(), cfg.crt_start) + timedelta(hours=1)
        ).time():
            width = spot * 0.004
            self._mock_crt[cfg.code] = (spot + width / 2, spot, spot - width / 2)

        crh, crm, crl = self._mock_crt.get(cfg.code, (spot * 1.002, spot, spot * 0.998))
        self._tick += 1
        phase = self._tick % 12
        if phase < 4:
            close = crl + (crm - crl) * 0.35
            low = crl - abs(crm - crl) * 0.05
            high = close + abs(crm - crl) * 0.15
        elif phase < 8:
            close = crm + abs(crh - crl) * 0.1
            low = crl + abs(crm - crl) * 0.2
            high = close + abs(crh - crl) * 0.08
        else:
            close = min(crh - abs(crh - crl) * 0.05, crm + abs(crh - crl) * 0.25)
            low = close - abs(crh - crl) * 0.06
            high = close + abs(crh - crl) * 0.04

        ts = now - timedelta(minutes=minutes)
        return [
            {
                "timestamp": ts.isoformat(),
                "open": close - (crm - crl) * 0.02,
                "high": high,
                "low": low,
                "close": close,
                "volume": 50_000,
            }
        ]

    def resolve_option(self, spot: float, direction: str) -> dict[str, Any] | None:
        cfg = INDEX_CONFIGS["NIFTY"]
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


class SMCCRTEngine:
    def __init__(self) -> None:
        self.client = SMCCRTMarketClient()
        self.states = {code: InstrumentState(config=cfg) for code, cfg in SMC_CRT_INSTRUMENTS.items()}
        logger.info(
            "SMC+CRT engine started (paper=%s mock=%s nifty only lot=%d session_end=%02d:%02d IST)",
            PAPER_TRADING,
            MOCK_MODE,
            LOTS_PER_TRADE,
            SESSION_END.hour,
            SESSION_END.minute,
        )

    def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                logger.exception("SMC+CRT tick failed: %s", exc)
            time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))

    def tick(self) -> None:
        now = datetime.now(IST)
        self.client.refresh_token()
        if now.time() >= SESSION_END:
            self._publish_all(now, entries_blocked=True, block_reason="session closed")
            return

        entries_blocked = now.time() >= SQUARE_OFF_TIME
        for state in self.states.values():
            self._process_instrument(state, now, entries_blocked)
        self._publish_heartbeat(now)

    def _process_instrument(self, state: InstrumentState, now: datetime, entries_blocked: bool) -> None:
        cfg = state.config
        spot = self.client.get_spot(cfg)
        if spot is not None:
            state.spot = spot

        self._refresh_crt(state, now)
        if state.crt_ready:
            candles_5m = self.client.get_candles(cfg, CANDLE_5M) or []
            if candles_5m:
                self._update_sweep_flags(state, candles_5m)
                fvg = detect_bullish_fvg(candles_5m) or detect_bearish_fvg(candles_5m)
                if fvg:
                    state.last_fvg = fvg

            if state.position:
                self._manage_position(state, now)
            elif not entries_blocked:
                self._seek_entry(state, candles_5m, now)
            else:
                state.setup_label = "Square-off window — no new SMC entries"

        self._publish_state(state, now, entries_blocked)

    def _refresh_crt(self, state: InstrumentState, now: datetime) -> None:
        cfg = state.config
        crt_end = datetime.combine(now.date(), cfg.crt_start, tzinfo=IST) + timedelta(hours=1)
        if now < crt_end:
            state.setup_label = f"Building 1H CRT ({cfg.crt_start.strftime('%H:%M')}–{crt_end.strftime('%H:%M')} IST)"
            return

        if state.crt_ready and state.crh is not None:
            return

        candles_1h = self.client.get_candles(cfg, CANDLE_1H) or []
        if candles_1h:
            first = candles_1h[0]
            state.crh, state.crm, state.crl = crt_from_1h_candle(first)
        elif state.spot is not None:
            width = state.spot * 0.004
            state.crh = state.spot + width / 2
            state.crm = state.spot
            state.crl = state.spot - width / 2

        if state.crh is not None:
            state.crt_ready = True
            state.setup_label = "CRT locked — watching 5m FVG"
            state.signal_log.append(f"CRT set CRH={state.crh:.2f} CRM={state.crm:.2f} CRL={state.crl:.2f}")

    def _update_sweep_flags(self, state: InstrumentState, candles: list[dict[str, float]]) -> None:
        if state.crl is None or state.crh is None:
            return
        for candle in candles[-6:]:
            if float(candle["low"]) < state.crl:
                state.swept_low = True
            if float(candle["high"]) > state.crh:
                state.swept_high = True

    def _open_position(
        self,
        state: InstrumentState,
        *,
        direction: str,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        fvg: FVGZone,
        now: datetime,
    ) -> None:
        lot_size = INDEX_CONFIGS["NIFTY"].lot_size
        quantity = lot_size * LOTS_PER_TRADE
        contract = self.client.resolve_option(entry, direction)
        if contract is None:
            logger.error("SMC entry aborted — no Nifty option contract")
            return
        if not self.client.place_entry(str(contract.get("instrument_key") or ""), quantity):
            logger.error("SMC entry order failed")
            return

        state.position = SMCCRTPosition(
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            opened_at=now.isoformat(),
            fvg_low=fvg.low,
            fvg_high=fvg.high,
            instrument_key=str(contract.get("instrument_key") or ""),
            option_strike=int(contract["strike"]),
            option_type=str(contract["option_type"]),
            lot_size=lot_size,
            quantity=quantity,
        )
        state.trades_today += 1
        state.last_signal_fvg_ts = fvg.candle_ts
        option_label = f"{contract['strike']}{contract['option_type']}"
        state.setup_label = f"{direction} entry (Strategy Type 2)"
        msg = (
            f"{state.config.display} SMC {direction} @ {entry:.2f} via {option_label} x{LOTS_PER_TRADE} lot "
            f"SL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f} (book @ TP1)"
        )
        state.signal_log.append(msg)
        logger.info(msg)
        telegram_notifier.notify_trade_execution(
            index_name=f"{state.config.display} SMC ({option_label} x{LOTS_PER_TRADE})",
            trade_type=direction,
            entry_price=entry,
            target_price=tp1,
            sl_price=sl,
            tp2_price=tp2,
            component_sentiment="NEUTRAL",
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _seek_entry(self, state: InstrumentState, candles: list[dict[str, float]], now: datetime) -> None:
        if state.spot is None or state.crm is None or state.crl is None or state.crh is None:
            return
        if near_crm(state.spot, state.crm):
            state.setup_label = "Near CRM equilibrium — avoid chop"
            return

        fvg = state.last_fvg
        if not fvg or not candles:
            state.setup_label = "Watching for sweep + FVG"
            return
        if state.trades_today >= MAX_TRADES_PER_DAY:
            state.setup_label = f"Max {MAX_TRADES_PER_DAY} SMC trades/day reached"
            return
        if fvg.candle_ts == state.last_signal_fvg_ts:
            state.setup_label = f"Setup armed — {fvg.direction} FVG"
            return

        last = candles[-1]
        close = float(last["close"])

        if fvg.direction == "LONG" and state.swept_low and close > state.crl:
            entry = close
            sl = fvg.low
            tp1, tp2, _ = rr_book_targets(entry, sl, "LONG")
            if not rr_ok(entry, sl, state.crm, "LONG"):
                state.setup_label = "Long FVG seen — R:R < 1:2 to CRM"
                return
            self._open_position(
                state, direction="LONG", entry=entry, sl=sl, tp1=tp1, tp2=tp2, fvg=fvg, now=now
            )

        elif fvg.direction == "SHORT" and state.swept_high and close < state.crh:
            entry = close
            sl = fvg.high
            tp1, tp2, _ = rr_book_targets(entry, sl, "SHORT")
            if not rr_ok(entry, sl, state.crm, "SHORT"):
                state.setup_label = "Short FVG seen — R:R < 1:2 to CRM"
                return
            self._open_position(
                state, direction="SHORT", entry=entry, sl=sl, tp1=tp1, tp2=tp2, fvg=fvg, now=now
            )

    def _manage_position(self, state: InstrumentState, now: datetime) -> None:
        pos = state.position
        if pos is None or state.spot is None:
            return
        spot = state.spot
        exit_reason = ""
        if pos.direction == "LONG":
            if spot <= pos.sl_price:
                exit_reason = "SL hit"
            elif spot >= pos.tp1_price:
                exit_reason = "TP1 booked (1R)"
        else:
            if spot >= pos.sl_price:
                exit_reason = "SL hit"
            elif spot <= pos.tp1_price:
                exit_reason = "TP1 booked (1R)"

        if not exit_reason:
            return

        if pos.instrument_key:
            self.client.place_exit(pos.instrument_key, pos.quantity)

        pnl = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
        logger.info("%s SMC exit: %s @ spot %.2f", state.config.display, exit_reason, spot)
        state.signal_log.append(f"Exit: {exit_reason} @ {spot:.2f}")
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_SMC_CRT,
            strategy_id="smc_crt",
            symbol=state.config.code,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=spot,
            pnl_points=pnl,
            exit_reason=exit_reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
        )
        state.position = None
        state.setup_label = f"Flat after {exit_reason}"
        telegram_notifier.notify_trade_exit(
            index_name=f"{state.config.display} SMC ({pos.option_strike}{pos.option_type})",
            trade_type=pos.direction,
            exit_price=spot,
            pnl_points=pnl,
            reason=exit_reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _publish_all(self, now: datetime, entries_blocked: bool, block_reason: str = "") -> None:
        for state in self.states.values():
            self._publish_state(state, now, entries_blocked, block_reason)
        self._publish_heartbeat(now)

    def _publish_state(
        self,
        state: InstrumentState,
        now: datetime,
        entries_blocked: bool,
        block_reason: str = "",
    ) -> None:
        pos = state.position
        payload: dict[str, Any] = {
            "symbol": state.config.code,
            "display": state.config.display,
            "strategy": "SMC+CRT",
            "spot": state.spot,
            "crh": state.crh,
            "crm": state.crm,
            "crl": state.crl,
            "crt_ready": state.crt_ready,
            "setup_label": state.setup_label,
            "swept_low": state.swept_low,
            "swept_high": state.swept_high,
            "paper_trading": PAPER_TRADING,
            "entries_blocked": entries_blocked,
            "block_reason": block_reason,
            "trades_today": state.trades_today,
            "max_trades": MAX_TRADES_PER_DAY,
            "lots_per_trade": LOTS_PER_TRADE,
            "session_end_ist": SESSION_END.strftime("%H:%M"),
            "signals": state.signal_log[-8:],
            "instrument_key": self.client.instrument_key(state.config),
            "updated_at": now.isoformat(),
        }
        if state.last_fvg:
            payload["fvg"] = {
                "direction": state.last_fvg.direction,
                "low": state.last_fvg.low,
                "high": state.last_fvg.high,
                "candle_ts": state.last_fvg.candle_ts,
            }
        if pos:
            payload["position"] = {
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "sl_price": pos.sl_price,
                "tp1_price": pos.tp1_price,
                "tp2_price": pos.tp2_price,
                "fvg_low": pos.fvg_low,
                "fvg_high": pos.fvg_high,
                "option_strike": pos.option_strike,
                "option_type": pos.option_type,
                "quantity": pos.quantity,
                "opened_at": pos.opened_at,
            }
        key = cache_manager.SMC_CRT_STATE_KEY_TEMPLATE.format(symbol=state.config.code)
        cache_manager.set_json(key, payload, ttl_seconds=120)

    def _publish_heartbeat(self, now: datetime) -> None:
        cache_manager.set_json(
            cache_manager.SMC_CRT_HEARTBEAT_KEY,
            {
                "at": now.isoformat(),
                "paper_trading": PAPER_TRADING,
                "mock": MOCK_MODE,
                "session_end_ist": SESSION_END.strftime("%H:%M"),
                "instruments": list(SMC_CRT_INSTRUMENTS.keys()),
            },
            ttl_seconds=60,
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    SMCCRTEngine().run()


if __name__ == "__main__":
    main()
