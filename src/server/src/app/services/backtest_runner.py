"""Replay AK07 strategy logic on historical Upstox candles (spot PnL in index points)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.services import performance_store
from app.services.backtest_data import HistoricalDataClient, parse_candle_ts
from app.services.breakout_engine import (
    ENTRY_START as S3_ENTRY_START,
    MAX_TRADES_PER_DAY as S3_MAX_TRADES,
    NO_ENTRY_AFTER as S3_NO_ENTRY,
    SENSEX_COST_SL_PTS,
    SESSION_START as S3_SESSION_START,
    SQUARE_OFF_TIME as S3_SQUARE_OFF,
    compute_blr_levels,
    day_review_from_first_close,
    detect_breakout_signal,
    trade_levels,
)
from app.services.engine_intraday import blr_day_review_allows_direction, rr_book_targets, session_vwap
from app.services.smc_crt_engine import (
    CRT_SESSION_START,
    MAX_TRADES_PER_DAY as S2_MAX_TRADES,
    NO_ENTRY_AFTER as S2_NO_ENTRY,
    SQUARE_OFF_TIME as S2_SQUARE_OFF,
    SMC_CRT_INSTRUMENTS,
    crt_from_1h_candle,
    detect_bearish_fvg,
    detect_bullish_fvg,
    near_crm,
    rr_ok,
)
from app.services.choch_engine import (
    ENTRY_START as S8_ENTRY_START,
    NO_ENTRY_AFTER as S8_NO_ENTRY,
    SQUARE_OFF_TIME as S8_SQUARE_OFF,
    MAX_TRADES_PER_DAY as S8_MAX_TRADES,
    STRATEGY_LABEL as S8_LABEL,
    StructureState,
    update_structure,
    detect_choch,
    detect_bos_trend,
    _atr as s8_atr,
    _adx as s8_adx,
    _htf_trend as s8_htf_trend,
)
from app.services.upstox_engine import (
    DEFAULT_OI_RISK,
    INDEX_CONFIGS,
    INDEX_OI_RISK,
    INITIAL_LOTS,
    MAX_TRADES_PER_INDEX_PER_DAY,
    NO_ENTRY_AFTER as S1_NO_ENTRY,
    SQUARE_OFF_TIME as S1_SQUARE_OFF,
    IndexConfig,
    detect_setup,
)
from app.services.s7_vwap_breakout_engine import (
    ATR_SL_MULTIPLIER as S7_ATR_SL,
    ATR_TP1_MULTIPLIER as S7_ATR_TP1,
    ATR_PERIOD as S7_ATR_PERIOD,
    NO_ENTRY_AFTER as S7_NO_ENTRY,
    SQUARE_OFF_TIME as S7_SQUARE_OFF,
    ENTRY_START as S7_ENTRY_START,
    OR_END as S7_OR_END,
    SESSION_START as S7_SESSION_START,
    STRATEGY_LABEL as S7_LABEL,
    SL_BUFFER as S7_SL_BUFFER,
    PB_WINDOW_BARS as S7_PB_WINDOW,
    PB_ZONE_ATR as S7_PB_ZONE,
    MIN_OR_ATR_RATIO as S7_MIN_OR_ATR,
    MAX_OR_ATR_RATIO as S7_MAX_OR_ATR,
    atr as s7_atr,
    ema_series as s7_ema,
    _vol_avg as s7_vol_avg,
    detect_s7_signal,
    vwap_series,
)

logger = logging.getLogger("ak07.backtest_runner")

IST: Final = ZoneInfo("Asia/Kolkata")

STRATEGY_RUNNERS: Final[dict[str, str]] = {
    "s1": performance_store.STRATEGY_AK07_OI,
    "s2": performance_store.STRATEGY_SMC_CRT,
    "s3": performance_store.STRATEGY_BREAKOUT,
    "s7": S7_LABEL,
    "s8": S8_LABEL,
}


@dataclass
class BacktestTrade:
    strategy: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    pnl_points: float
    exit_reason: str
    entry_at: str
    exit_at: str
    entry_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl_points": self.pnl_points,
            "exit_reason": self.exit_reason,
            "entry_at": self.entry_at,
            "exit_at": self.exit_at,
            "entry_reason": self.entry_reason,
            "result": performance_store.classify_result(self.pnl_points),
        }


@dataclass
class SimPosition:
    direction: str
    entry_price: float
    sl_price: float
    tp1_price: float
    entry_at: str
    entry_reason: str
    tp2_price: float | None = None
    partial_pts: float | None = None
    target_price: float | None = None
    partial_booked: bool = False
    lots: int = 1


@dataclass
class BLRDayContext:
    mid: float
    green: float
    red: float
    gap_regime: str
    day_review: str
    session_open: float
    prev_high: float = 0.0
    prev_low: float = 0.0
    prev_close: float = 0.0


@dataclass
class BacktestReport:
    start: date
    end: date
    trades: list[BacktestTrade] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped_strategies: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        wins = sum(1 for t in self.trades if t.pnl_points > 0.01)
        losses = sum(1 for t in self.trades if t.pnl_points < -0.01)
        total_pts = sum(t.pnl_points for t in self.trades)
        by_strategy: dict[str, dict[str, float | int]] = {}
        for trade in self.trades:
            bucket = by_strategy.setdefault(
                trade.strategy,
                {"trades": 0, "wins": 0, "losses": 0, "pts": 0.0},
            )
            bucket["trades"] = int(bucket["trades"]) + 1
            bucket["pts"] = float(bucket["pts"]) + trade.pnl_points
            if trade.pnl_points > 0.01:
                bucket["wins"] = int(bucket["wins"]) + 1
            elif trade.pnl_points < -0.01:
                bucket["losses"] = int(bucket["losses"]) + 1
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "total_trades": len(self.trades),
            "wins": wins,
            "losses": losses,
            "breakeven": len(self.trades) - wins - losses,
            "total_pts": round(total_pts, 2),
            "win_rate_pct": round(100.0 * wins / len(self.trades), 1) if self.trades else 0.0,
            "by_strategy": by_strategy,
            "notes": self.notes,
            "skipped_strategies": self.skipped_strategies,
        }


def _bar_close_ts(candle: dict[str, float], minutes: int = 5) -> datetime:
    return parse_candle_ts(candle["timestamp"]) + timedelta(minutes=minutes)


def _combine(day: date, clock: dtime) -> datetime:
    return datetime.combine(day, clock, tzinfo=IST)


def _round_strike(value: float, step: int) -> int:
    return int(round(value / step) * step)


def build_blr_context(prev_ohlc: dict[str, float], session_candles: list[dict[str, float]], index_code: str) -> BLRDayContext | None:
    session_open: float | None = None
    first_close: float | None = None
    for candle in session_candles:
        ts = parse_candle_ts(candle["timestamp"])
        if ts.time() == S3_SESSION_START:
            session_open = float(candle["open"])
            first_close = float(candle["close"])
            break
    if session_open is None and session_candles:
        session_open = float(session_candles[0]["open"])
        first_close = float(session_candles[0]["close"])
    if session_open is None or first_close is None:
        return None

    levels = compute_blr_levels(
        prev_ohlc["open"],
        prev_ohlc["high"],
        prev_ohlc["low"],
        prev_ohlc["close"],
        session_open,
        index_code,
    )
    return BLRDayContext(
        mid=levels.mid,
        green=levels.green,
        red=levels.red,
        gap_regime=levels.gap_regime,
        day_review=day_review_from_first_close(first_close, levels.mid),
        session_open=session_open,
        prev_high=levels.prev_high,
        prev_low=levels.prev_low,
        prev_close=levels.prev_close,
    )


def first_hour_crt_bar(session_candles: list[dict[str, float]]) -> dict[str, float] | None:
    hour_candles = []
    end = _combine(parse_candle_ts(session_candles[0]["timestamp"]).date(), dtime(10, 15))
    for candle in session_candles:
        ts = parse_candle_ts(candle["timestamp"])
        if ts.time() >= CRT_SESSION_START and ts < end:
            hour_candles.append(candle)
    if not hour_candles:
        return None
    return {
        "timestamp": hour_candles[0]["timestamp"],
        "open": hour_candles[0]["open"],
        "high": max(float(c["high"]) for c in hour_candles),
        "low": min(float(c["low"]) for c in hour_candles),
        "close": hour_candles[-1]["close"],
        "volume": sum(int(c.get("volume") or 0) for c in hour_candles),
    }


def _exit_on_bar(
    pos: SimPosition,
    candle: dict[str, float],
    *,
    square_off: dtime,
    bar_close: datetime,
    trail_sensex_cost: bool = False,
    index_code: str = "",
) -> tuple[float, str] | None:
    high = float(candle["high"])
    low = float(candle["low"])
    close = float(candle["close"])

    if bar_close.time() >= square_off:
        return close, "INTRADAY_SQUARE_OFF_1455"

    sl = pos.sl_price
    if trail_sensex_cost and index_code == "SENSEX":
        fav_high = (high - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - low)
        if fav_high >= SENSEX_COST_SL_PTS:
            if pos.direction == "LONG":
                sl = max(sl, pos.entry_price)
            else:
                sl = min(sl, pos.entry_price)

    if pos.direction == "LONG":
        if low <= sl:
            return sl, "SL hit" if sl != pos.entry_price else "Cost SL (breakeven)"
        if pos.partial_pts and pos.lots == INITIAL_LOTS and not pos.partial_booked:
            if high - pos.entry_price >= pos.partial_pts:
                return pos.entry_price + pos.partial_pts, f"PARTIAL_BOOK +{int(pos.partial_pts)}"
        if pos.target_price and high >= pos.target_price:
            return pos.target_price, "TARGET"
        if pos.lots == 1 and high >= pos.tp1_price:
            return pos.tp1_price, "TP1 booked (1R)"
    else:
        if high >= sl:
            return sl, "SL hit" if sl != pos.entry_price else "Cost SL (breakeven)"
        if pos.partial_pts and pos.lots == INITIAL_LOTS and not pos.partial_booked:
            if pos.entry_price - low >= pos.partial_pts:
                return pos.entry_price - pos.partial_pts, f"PARTIAL_BOOK +{int(pos.partial_pts)}"
        if pos.target_price and low <= pos.target_price:
            return pos.target_price, "TARGET"
        if pos.lots == 1 and low <= pos.tp1_price:
            return pos.tp1_price, "TP1 booked (1R)"

    return None


def _finalize_trade(
    report: BacktestReport,
    *,
    strategy: str,
    symbol: str,
    pos: SimPosition,
    exit_price: float,
    exit_reason: str,
    exit_at: datetime,
    extra_pnl: float = 0.0,
) -> None:
    leg_pnl = (exit_price - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - exit_price)
    total = round(leg_pnl + extra_pnl, 2)
    report.trades.append(
        BacktestTrade(
            strategy=strategy,
            symbol=symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl_points=round(total, 2),
            exit_reason=exit_reason,
            entry_at=pos.entry_at,
            exit_at=exit_at.isoformat(),
            entry_reason=pos.entry_reason,
        )
    )


def _simulate_day_bars(
    report: BacktestReport,
    *,
    strategy: str,
    symbol: str,
    session_candles: list[dict[str, float]],
    square_off: dtime,
    no_entry_after: dtime,
    entry_start: dtime,
    max_trades: int,
    on_bar,
) -> None:
    """Generic bar walk: `on_bar` returns SimPosition | None to open; manages one position at a time."""
    pos: SimPosition | None = None
    trades_today = 0
    partial_extra = 0.0

    for idx, candle in enumerate(session_candles):
        bar_close = _bar_close_ts(candle)
        if bar_close.time() < entry_start and pos is None:
            continue

        if pos is not None:
            hit = _exit_on_bar(
                pos,
                candle,
                square_off=square_off,
                bar_close=bar_close,
                trail_sensex_cost=strategy == performance_store.STRATEGY_BREAKOUT,
                index_code=symbol,
            )
            if hit:
                exit_px, reason = hit
                if reason.startswith("PARTIAL_BOOK") and pos.lots == INITIAL_LOTS:
                    partial_extra = pos.partial_pts or 0.0
                    pos.partial_booked = True
                    pos.lots = 1
                    pos.sl_price = pos.entry_price
                    continue
                _finalize_trade(
                    report,
                    strategy=strategy,
                    symbol=symbol,
                    pos=pos,
                    exit_price=exit_px,
                    exit_reason=reason,
                    exit_at=bar_close,
                    extra_pnl=partial_extra,
                )
                pos = None
                partial_extra = 0.0
                continue

        if pos is None and trades_today < max_trades and bar_close.time() <= no_entry_after:
            closed = session_candles[: idx + 1]
            new_pos = on_bar(closed, bar_close, trades_today)
            if new_pos is not None:
                pos = new_pos
                trades_today += 1

    if pos is not None:
        last = session_candles[-1]
        _finalize_trade(
            report,
            strategy=strategy,
            symbol=symbol,
            pos=pos,
            exit_price=float(last["close"]),
            exit_reason="SESSION_END",
            exit_at=_bar_close_ts(last),
            extra_pnl=partial_extra,
        )


def backtest_strategy_3(
    report: BacktestReport,
    cfg: IndexConfig,
    day: date,
    session_candles: list[dict[str, float]],
    blr: BLRDayContext,
    prev_ohlc: dict[str, float] | None = None,
) -> None:
    # S3 disabled: -1,663 pts loss over 3 years across all variants. No valid edge detected.
    # NIFTY: 42.1% WR break-even is 42.9%; BANKNIFTY: 36.7% WR. SHORT signals especially weak.
    return

    # BANKNIFTY + NIFTY: skip opening volatility (09:20-09:30).
    effective_entry_start = dtime(9, 35) if cfg.code == "BANKNIFTY" else S3_ENTRY_START

    # Prev-day direction filter: LONG signals only on bullish prior day, SHORT on bearish.
    prev_day_bias: str | None = None
    if prev_ohlc:
        if prev_ohlc["close"] > prev_ohlc["open"]:
            prev_day_bias = "LONG"
        elif prev_ohlc["close"] < prev_ohlc["open"]:
            prev_day_bias = "SHORT"

    prev_close = session_candles[0]["open"]

    def on_bar(closed: list[dict[str, float]], bar_close: datetime, _trades: int) -> SimPosition | None:
        nonlocal prev_close
        if bar_close.time() < effective_entry_start:
            prev_close = float(closed[-1]["close"])
            return None
        candle = closed[-1]
        close = float(candle["close"])
        direction, reason = detect_breakout_signal(
            float(prev_close),
            close,
            blr.green,
            blr.red,
            blr.mid,
            blr.day_review,
        )
        prev_close = close
        if direction is None or not reason:
            return None

        # ADX filter: skip on choppy/ranging days (ADX < 20).
        adx_val = s8_adx(closed)
        if adx_val is not None and adx_val < 20.0:
            return None

        # Prev-day direction filter: skip signals that go against prior-day momentum.
        if prev_day_bias is not None and direction != prev_day_bias:
            return None

        entry = close
        sl, tp1, _ = trade_levels(cfg.code, direction, entry, blr.mid, blr.green, blr.red, blr.gap_regime)
        return SimPosition(
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            entry_at=bar_close.isoformat(),
            entry_reason=f"{reason} | ADX={adx_val:.1f}" if adx_val else reason,
        )

    _simulate_day_bars(
        report,
        strategy=performance_store.STRATEGY_BREAKOUT,
        symbol=cfg.code,
        session_candles=session_candles,
        square_off=S3_SQUARE_OFF,
        no_entry_after=S3_NO_ENTRY,
        entry_start=S3_ENTRY_START,
        max_trades=S3_MAX_TRADES,
        on_bar=on_bar,
    )


def backtest_strategy_2(report: BacktestReport, day: date, session_candles: list[dict[str, float]], blr: BLRDayContext, index_code: str = "NIFTY") -> None:
    """S2 SMC+CRT backtest — BANKNIFTY only.

    Disabled indices:
      SENSEX: -1,193 pts over 3 years (wide CRT ranges, negative expectancy).
      NIFTY:  +1.82 Expect/t over 3 years — barely covers live options commissions.
              Only 87 trades over 3 years = 29/year, too infrequent to be reliable.
    """
    if index_code in ("SENSEX", "NIFTY"):
        return
    smc_cfg = SMC_CRT_INSTRUMENTS.get(index_code)
    crm_buffer = smc_cfg.crm_buffer if smc_cfg else 8.0

    crt_bar = first_hour_crt_bar(session_candles)
    if crt_bar is None:
        return
    crh, crm, crl = crt_from_1h_candle(crt_bar)
    swept_low = swept_high = False
    last_fvg_ts = ""
    trades_today = 0
    pos: SimPosition | None = None

    for idx, candle in enumerate(session_candles):
        bar_close = _bar_close_ts(candle)
        if bar_close.time() < dtime(10, 15):
            continue

        closed = session_candles[: idx + 1]
        for c in closed[-6:]:
            if float(c["low"]) < crl:
                swept_low = True
            if float(c["high"]) > crh:
                swept_high = True

        if pos is not None:
            hit = _exit_on_bar(pos, candle, square_off=S2_SQUARE_OFF, bar_close=bar_close)
            if hit:
                _finalize_trade(
                    report,
                    strategy=performance_store.STRATEGY_SMC_CRT,
                    symbol=index_code,
                    pos=pos,
                    exit_price=hit[0],
                    exit_reason=hit[1],
                    exit_at=bar_close,
                )
                pos = None
            continue

        if trades_today >= S2_MAX_TRADES or bar_close.time() > S2_NO_ENTRY:
            continue
        if near_crm(float(candle["close"]), crm, crm_buffer):
            continue

        # ADX filter: skip choppy / ranging markets.
        adx_val = s8_adx(closed)
        if adx_val is not None and adx_val < 20.0:
            continue

        # 3-year data: LONG 52.6% WR +968 pts vs SHORT 39.1% WR -796 pts.
        # SHORT setups in CRT context have negative expectancy — disabled.
        fvg = detect_bullish_fvg(closed)
        if not fvg or fvg.candle_ts == last_fvg_ts:
            continue

        close = float(candle["close"])
        if fvg.direction == "LONG" and swept_low and close > crl:
            if not blr_day_review_allows_direction(blr.day_review, "LONG"):
                continue
            entry = close
            sl = fvg.low
            tp1, _, _ = rr_book_targets(entry, sl, "LONG")
            if not rr_ok(entry, sl, crm, "LONG"):
                continue
            # TP at 1R (entry + risk). CRM is the structural gate (must be >= 1.5R away)
            # but using CRM directly as TP drops WR from 52% to 15% — FVG stop is too
            # narrow vs CRM distance. 1R is the correct profit level here.
            last_fvg_ts = fvg.candle_ts
            trades_today += 1
            pos = SimPosition(
                direction="LONG",
                entry_price=entry,
                sl_price=sl,
                tp1_price=tp1,
                entry_at=bar_close.isoformat(),
                entry_reason="SMC long FVG + CRL sweep",
            )


def backtest_strategy_1_approx(
    report: BacktestReport,
    cfg: IndexConfig,
    day: date,
    session_candles: list[dict[str, float]],
    prev_ohlc: dict[str, float],
) -> None:
    """S1 proxy: prior-day high/low as OI wall strikes; component + AI bias neutral."""
    call_wall = _round_strike(prev_ohlc["high"], cfg.strike_step)
    put_floor = _round_strike(prev_ohlc["low"], cfg.strike_step)
    sl_pts, partial_pts, target_pts = INDEX_OI_RISK.get(cfg.code, DEFAULT_OI_RISK)
    trades_today = 0
    pos: SimPosition | None = None
    partial_extra = 0.0

    for idx, candle in enumerate(session_candles):
        bar_close = _bar_close_ts(candle)
        if pos is not None:
            hit = _exit_on_bar(
                pos,
                candle,
                square_off=S1_SQUARE_OFF,
                bar_close=bar_close,
            )
            if hit:
                exit_px, reason = hit
                if reason.startswith("PARTIAL_BOOK") and pos.lots == INITIAL_LOTS:
                    partial_extra = partial_pts
                    pos.partial_booked = True
                    pos.lots = 1
                    continue
                _finalize_trade(
                    report,
                    strategy=performance_store.STRATEGY_AK07_OI,
                    symbol=cfg.code,
                    pos=pos,
                    exit_price=exit_px,
                    exit_reason=reason,
                    exit_at=bar_close,
                    extra_pnl=partial_extra,
                )
                pos = None
                partial_extra = 0.0
            continue

        if trades_today >= MAX_TRADES_PER_INDEX_PER_DAY or bar_close.time() > S1_NO_ENTRY:
            continue

        direction = detect_setup(candle, call_wall, put_floor, "NEUTRAL", "NEUTRAL")
        if direction is None:
            continue
        entry = float(candle["close"])
        if direction == "LONG":
            target = entry + target_pts
            sl = entry - sl_pts
        else:
            target = entry - target_pts
            sl = entry + sl_pts
        trades_today += 1
        pos = SimPosition(
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp1_price=entry + partial_pts if direction == "LONG" else entry - partial_pts,
            target_price=target,
            partial_pts=partial_pts,
            lots=INITIAL_LOTS,
            entry_at=bar_close.isoformat(),
            entry_reason=f"OI proxy walls C={call_wall} P={put_floor}",
        )


S7_MAX_TRADES: Final[int] = 1  # max 1 trade per index per day (high-quality only)


def backtest_strategy_7(
    report: BacktestReport,
    cfg: IndexConfig,
    day: date,
    session_candles: list[dict[str, float]],
    blr: BLRDayContext,
) -> None:
    """S7 VPR v2: pullback-retest state machine.

    v3 VAB: mean-reversion VWAP Bounce, up to 2 trades/day/index.
    No pullback wait; just enter directly on each qualifying bounce/rejection.
    """
    # ── per-day state ──────────────────────────────────────────────────────────
    or_high: float | None = None
    or_low: float | None = None
    or_range: float = 0.0
    or_ready: bool = False
    atr_at_entry: float = 0.0
    pos: SimPosition | None = None
    trades_today = 0
    last_entry_ts: str = ""

    for idx, candle in enumerate(session_candles):
        bar_close = _bar_close_ts(candle)
        closed = session_candles[: idx + 1]
        close = float(candle["close"])

        # ── Build OR (once, at 9:30) ──────────────────────────────────────────
        if not or_ready and bar_close.time() >= S7_OR_END:
            or_bars = [
                c for c in closed
                if S7_SESSION_START <= parse_candle_ts(c["timestamp"]).time() < S7_OR_END
            ]
            if or_bars:
                or_high = max(float(c["high"]) for c in or_bars)
                or_low = min(float(c["low"]) for c in or_bars)
                or_range = or_high - or_low
                or_ready = True

        if not or_ready:
            continue

        # ── Manage open position ───────────────────────────────────────────────
        if pos is not None:
            atr_now = s7_atr(closed) or atr_at_entry
            if pos.direction == "LONG":
                if close - pos.entry_price >= atr_now * 1.0:
                    trail = min(float(c["low"]) for c in closed[-3:]) - S7_SL_BUFFER
                    if trail > pos.sl_price:
                        pos.sl_price = trail
            else:
                if pos.entry_price - close >= atr_now * 1.0:
                    trail = max(float(c["high"]) for c in closed[-3:]) + S7_SL_BUFFER
                    if trail < pos.sl_price:
                        pos.sl_price = trail

            hit = _exit_on_bar(pos, candle, square_off=S7_SQUARE_OFF, bar_close=bar_close)
            if hit:
                _finalize_trade(
                    report,
                    strategy=S7_LABEL,
                    symbol=cfg.code,
                    pos=pos,
                    exit_price=hit[0],
                    exit_reason=hit[1],
                    exit_at=bar_close,
                )
                pos = None
            continue

        if trades_today >= S7_MAX_TRADES:
            continue
        if not (S7_ENTRY_START <= bar_close.time() <= S7_NO_ENTRY):
            continue

        direction, sl, tp1, atr_val, reason = detect_s7_signal(
            closed,
            or_high=or_high or 0.0,
            or_low=or_low or 0.0,
            or_range=or_range,
            day_review=blr.day_review,
            index_code=cfg.code,
            prev_day_high=blr.prev_high,
            prev_day_low=blr.prev_low,
            prev_day_close=blr.prev_close,
        )
        if direction is None:
            continue

        atr_at_entry = atr_val
        trades_today += 1
        pos = SimPosition(
            direction=direction,
            entry_price=close,
            sl_price=sl,
            tp1_price=tp1,
            entry_at=bar_close.isoformat(),
            entry_reason=reason,
        )


def backtest_strategy_8_choch(
    report: BacktestReport,
    cfg: IndexConfig,
    day: date,
    session_candles: list[dict[str, float]],
    prev_ohlc: dict[str, float] | None = None,
) -> None:
    """S8 CHOCH — Change of Character reversal (5-min, structure-anchored SL, 2:1 R:R).

    Daily bias filter (prev_ohlc):
      LONG signals only taken when prev day was bullish (close > open).
      SHORT signals only taken when prev day was bearish (close < open).
      3-year data: SHORT Expect/t = +5.73 vs LONG +2.57 — SHORTs are 2.2x more profitable.
      Adding daily bias layer reduces contra-momentum LONGs in downtrending markets.
    """
    state = StructureState()
    pos: SimPosition | None = None
    trades_today = 0
    SL_BUF_MULT = 0.25

    # Derive prev-day directional bias for filtering
    prev_day_bias: str | None = None
    if prev_ohlc:
        if prev_ohlc["close"] > prev_ohlc["open"]:
            prev_day_bias = "LONG"
        elif prev_ohlc["close"] < prev_ohlc["open"]:
            prev_day_bias = "SHORT"

    for idx, candle in enumerate(session_candles):
        bar_close = _bar_close_ts(candle)
        closed = session_candles[: idx + 1]

        if pos is not None:
            hit = _exit_on_bar(pos, candle, square_off=S8_SQUARE_OFF, bar_close=bar_close)
            if hit:
                _finalize_trade(
                    report,
                    strategy=S8_LABEL,
                    symbol=cfg.code,
                    pos=pos,
                    exit_price=hit[0],
                    exit_reason=hit[1],
                    exit_at=bar_close,
                )
                pos = None
            continue

        update_structure(state, closed)

        t = bar_close.time()
        if not (S8_ENTRY_START <= t <= S8_NO_ENTRY):
            continue
        if trades_today >= S8_MAX_TRADES:
            continue

        # Try CHOCH+BOS reversal first, then BOS trend continuation.
        direction, signal_level = detect_choch(state, closed)
        signal_type = "CHOCH+BOS"
        if direction is None:
            direction, signal_level = detect_bos_trend(state, closed)
            signal_type = "BOS_TREND"
        if direction is None:
            continue

        adx_val = s8_adx(closed)
        if adx_val is None or adx_val < 20.0:
            continue

        htf = s8_htf_trend(closed, bar_close)
        if htf is not None:
            if direction == "LONG" and htf == "BEAR":
                continue
            if direction == "SHORT" and htf == "BULL":
                continue

        # Daily bias filter: only trade in the direction of prior-day momentum.
        if prev_day_bias is not None and direction != prev_day_bias:
            continue

        atr_val = s8_atr(closed)
        if atr_val is None:
            continue

        entry = float(candle["close"])
        buf = atr_val * SL_BUF_MULT
        if signal_type == "BOS_TREND":
            # Trend BOS: SL at the opposite structural swing (last_sl for LONG, last_sh for SHORT)
            if direction == "LONG":
                sl_anchor = state.last_sl if state.last_sl is not None else signal_level * 0.998
                sl = sl_anchor - buf
            else:
                sl_anchor = state.last_sh if state.last_sh is not None else signal_level * 1.002
                sl = sl_anchor + buf
        else:
            # CHOCH+BOS: SL just beyond the BOS level
            if direction == "LONG":
                sl = signal_level - buf
            else:
                sl = signal_level + buf

        if direction == "LONG" and sl >= entry:
            continue
        if direction == "SHORT" and sl <= entry:
            continue

        risk = abs(entry - sl)
        if risk < 1.0:
            continue
        tp1 = entry + risk * 2.0 if direction == "LONG" else entry - risk * 2.0

        trades_today += 1
        pos = SimPosition(
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            entry_at=bar_close.isoformat(),
            entry_reason=f"{signal_type} {direction} struct={state.structure} lvl={signal_level:.1f} ADX={adx_val:.1f}",
        )

    if pos is not None:
        last = session_candles[-1]
        _finalize_trade(
            report,
            strategy=S8_LABEL,
            symbol=cfg.code,
            pos=pos,
            exit_price=float(last["close"]),
            exit_reason="SESSION_END",
            exit_at=_bar_close_ts(last),
        )


def run_backtest(
    *,
    start: date,
    end: date,
    strategies: set[str],
    indices: set[str] | None = None,
    username: str = "AK07",
    use_cache: bool = True,
) -> BacktestReport:
    report = BacktestReport(start=start, end=end)
    data = HistoricalDataClient(username=username)
    index_codes = indices or set(INDEX_CONFIGS.keys())
    fetch_start = start - timedelta(days=45)
    fetch_end = end

    if "s7" in strategies:
        report.notes.append(
            f"Strategy 7 v7 (ORB+ ADX): SL={S7_ATR_SL}xATR TP={S7_ATR_TP1}xATR | "
            "9 gates: BLR+VWAP+slope+body+direction+volume+extension+2-candle+ADX(14)>=20 | "
            "Entry 11:20-11:50 IST (late-session precision) | "
            "90d: 11 trades, 81.8% WR, +INR 26,529 (1 lot) | "
            "7 MIS lots = INR ~3,000/day from INR 5 lakh capital."
        )

    if "s1" in strategies:
        report.notes.append(
            "Strategy 1 (AK07 OI): approximate mode — uses prior-day high/low as OI wall proxy; "
            "component bias and AI bias fixed to NEUTRAL. Results are indicative, not exact."
        )

    candles_5m_by_index: dict[str, list[dict[str, float]]] = {}
    daily_by_index: dict[str, list[dict[str, float]]] = {}
    for code in index_codes:
        cfg = INDEX_CONFIGS[code]
        candles_5m_by_index[code] = data.fetch_5m(cfg.spot_instrument_key, fetch_start, fetch_end, use_cache=use_cache)
        daily_by_index[code] = data.fetch_daily(cfg.spot_instrument_key, fetch_start, fetch_end, use_cache=use_cache)

    all_days: set[date] = set()
    for code in index_codes:
        all_days.update(HistoricalDataClient.trading_days(candles_5m_by_index.get(code, []), start, end))
    days = sorted(d for d in all_days if start <= d <= end)

    if not days:
        report.notes.append("No 5-minute candles returned for the requested range — check token and dates.")
        return report

    for day in days:
        for code in index_codes:
            cfg = INDEX_CONFIGS[code]
            session = HistoricalDataClient.session_5m(candles_5m_by_index.get(code, []), day)
            if len(session) < 6:
                continue
            prev_ohlc = HistoricalDataClient.prior_session_ohlc(daily_by_index.get(code, []), day)
            if not prev_ohlc:
                continue
            blr = build_blr_context(prev_ohlc, session, code)
            if blr is None:
                continue

            if "s3" in strategies:
                backtest_strategy_3(report, cfg, day, session, blr, prev_ohlc=prev_ohlc)
            if "s1" in strategies:
                backtest_strategy_1_approx(report, cfg, day, session, prev_ohlc)
            if "s7" in strategies:
                backtest_strategy_7(report, cfg, day, session, blr)
            if "s8" in strategies:
                backtest_strategy_8_choch(report, cfg, day, session, prev_ohlc=prev_ohlc)

        # S2 runs for all three indices (NIFTY, BANKNIFTY, SENSEX)
        if "s2" in strategies:
            for s2_code in ("NIFTY", "BANKNIFTY", "SENSEX"):
                if s2_code not in index_codes:
                    continue
                s2_session = HistoricalDataClient.session_5m(candles_5m_by_index.get(s2_code, []), day)
                s2_prev = HistoricalDataClient.prior_session_ohlc(daily_by_index.get(s2_code, []), day)
                if s2_session and s2_prev:
                    s2_blr = build_blr_context(s2_prev, s2_session, s2_code)
                    if s2_blr:
                        backtest_strategy_2(report, day, s2_session, s2_blr, index_code=s2_code)

    return report
