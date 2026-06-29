"""Backtest harness for ``velocity_breakout.check_velocity_breakout``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services.backtest_data import HistoricalDataClient, parse_candle_ts
from app.services.backtest_runner import _bar_close_ts
from app.services.velocity_breakout import check_velocity_breakout
from app.services.upstox_engine import INDEX_CONFIGS

IST = ZoneInfo("Asia/Kolkata")
SESSION_START = time(9, 15)
ENTRY_START = time(9, 25)
ENTRY_END = time(14, 45)
SQUARE_OFF = time(15, 25)
DELTA = 0.5
MIN_WARMUP_BARS = 38  # sensitivity(5) + 13 + 20
SENSITIVITY = 5


def _effective_volume(candle: dict[str, float]) -> float:
    vol = float(candle.get("volume") or 0)
    if vol > 0:
        return vol
    high = float(candle.get("high") or candle.get("close") or 0)
    low = float(candle.get("low") or candle.get("close") or 0)
    close = float(candle.get("close") or 0)
    return max((high - low) * close, 1.0)


def _prepare_candles(candles: list[dict[str, float]]) -> list[dict]:
    out: list[dict] = []
    for candle in candles:
        row = dict(candle)
        row["volume"] = _effective_volume(candle)
        out.append(row)
    return out


def _premium_risk(symbol: str) -> tuple[float, float]:
    if "BANKNIFTY" in symbol:
        return 150.0, 45.0
    if "SENSEX" in symbol:
        return 120.0, 35.0
    if "NIFTY" in symbol:
        return 60.0, 18.0
    return 60.0, 18.0


@dataclass
class VelocityTrade:
    symbol: str
    day: str
    side: str
    entry_spot: float
    exit_spot: float
    pnl_premium_pts: float
    exit_reason: str
    signal_type: str
    trigger_level: float | None = None


@dataclass
class VelocitySummary:
    symbol: str
    trades: list[VelocityTrade] = field(default_factory=list)
    days_scanned: int = 0
    signal_bars_ce: int = 0
    signal_bars_pe: int = 0
    hold_bars: int = 0

    @property
    def total_premium_pts(self) -> float:
        return round(sum(t.pnl_premium_pts for t in self.trades), 2)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_premium_pts > 0.01)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl_premium_pts < -0.01)

    @property
    def win_rate(self) -> float:
        return (100.0 * self.wins / len(self.trades)) if self.trades else 0.0

    def inr_pnl(self) -> float:
        lot = INDEX_CONFIGS[self.symbol].lot_size
        return round(self.total_premium_pts * lot, 2)


def _session_candles(candles: list[dict[str, float]], day: date) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for candle in candles:
        ts = parse_candle_ts(str(candle["timestamp"]))
        if ts.date() != day:
            continue
        if SESSION_START <= ts.time() <= SQUARE_OFF:
            out.append(candle)
    return out


def _prior_bars(candles: list[dict[str, float]], day: date, count: int) -> list[dict[str, float]]:
    prior: list[dict[str, float]] = []
    for candle in candles:
        ts = parse_candle_ts(str(candle["timestamp"]))
        if ts.date() >= day:
            break
        if ts.weekday() < 5:
            prior.append(candle)
    return prior[-count:]


def _manage_trade(
    symbol: str,
    day: date,
    side: str,
    entry_spot: float,
    entry_time: datetime,
    session: list[dict[str, float]],
    signal_type: str,
    trigger_level: float | None,
) -> VelocityTrade:
    premium_target, premium_sl = _premium_risk(symbol)
    target_move = premium_target / DELTA
    sl_move = premium_sl / DELTA

    exit_spot = entry_spot
    exit_reason = "EOD"

    for candle in session:
        bar_close = _bar_close_ts(candle)
        if bar_close <= entry_time:
            continue
        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])

        if side == "CE":
            if entry_spot - low >= sl_move:
                exit_spot = entry_spot - sl_move
                exit_reason = "PREMIUM_SL"
                break
            if high - entry_spot >= target_move:
                exit_spot = entry_spot + target_move
                exit_reason = "PREMIUM_TARGET"
                break
        else:
            if high - entry_spot >= sl_move:
                exit_spot = entry_spot + sl_move
                exit_reason = "PREMIUM_SL"
                break
            if entry_spot - low >= target_move:
                exit_spot = entry_spot - target_move
                exit_reason = "PREMIUM_TARGET"
                break

        if bar_close.time() >= SQUARE_OFF:
            exit_spot = close
            exit_reason = "EOD"
            break
        exit_spot = close

    if side == "CE":
        pnl = (exit_spot - entry_spot) * DELTA
    else:
        pnl = (entry_spot - exit_spot) * DELTA

    return VelocityTrade(
        symbol=symbol,
        day=day.isoformat(),
        side=side,
        entry_spot=round(entry_spot, 2),
        exit_spot=round(exit_spot, 2),
        pnl_premium_pts=round(pnl, 2),
        exit_reason=exit_reason,
        signal_type=signal_type,
        trigger_level=trigger_level,
    )


def _simulate_day(
    symbol: str,
    day: date,
    session: list[dict[str, float]],
    prior: list[dict[str, float]],
    summary: VelocitySummary,
) -> VelocityTrade | None:
    if len(session) < 3:
        return None

    combined = prior + session
    trade: VelocityTrade | None = None

    for i, candle in enumerate(session):
        bar_close = _bar_close_ts(candle)
        if trade is not None:
            break
        if bar_close.time() < ENTRY_START or bar_close.time() > ENTRY_END:
            continue

        global_end = len(prior) + i + 1
        window = _prepare_candles(combined[:global_end])
        if len(window) < MIN_WARMUP_BARS:
            continue

        signal = check_velocity_breakout(window, sensitivity=SENSITIVITY)
        status = signal.get("status", "HOLD")

        if status == "BUY_CE":
            summary.signal_bars_ce += 1
            trade = _manage_trade(
                symbol,
                day,
                "CE",
                float(candle["close"]),
                bar_close,
                session,
                str(signal.get("signal_type", "")),
                float(signal["trigger_level"]) if signal.get("trigger_level") is not None else None,
            )
        elif status == "BUY_PE":
            summary.signal_bars_pe += 1
            trade = _manage_trade(
                symbol,
                day,
                "PE",
                float(candle["close"]),
                bar_close,
                session,
                str(signal.get("signal_type", "")),
                float(signal["trigger_level"]) if signal.get("trigger_level") is not None else None,
            )
        elif status == "HOLD":
            summary.hold_bars += 1

    return trade


def run_velocity_breakout_backtest(
    start: date,
    end: date,
    symbols: list[str] | None = None,
    username: str = "AK07",
) -> dict[str, Any]:
    symbols = symbols or ["NIFTY", "BANKNIFTY", "SENSEX"]
    client = HistoricalDataClient(username=username)
    warmup = MIN_WARMUP_BARS + 10
    fetch_start = start - timedelta(days=14)

    candles_by_symbol: dict[str, list[dict[str, float]]] = {}
    for code in symbols:
        cfg = INDEX_CONFIGS[code]
        candles_by_symbol[code] = client.fetch_5m(
            cfg.spot_instrument_key, fetch_start, end, use_cache=True
        )

    summaries = {s: VelocitySummary(symbol=s) for s in symbols}
    day = start
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        for code in symbols:
            summary = summaries[code]
            summary.days_scanned += 1
            session = _session_candles(candles_by_symbol[code], day)
            prior = _prior_bars(candles_by_symbol[code], day, warmup)
            if not session:
                continue
            trade = _simulate_day(code, day, session, prior, summary)
            if trade:
                summary.trades.append(trade)
        day += timedelta(days=1)

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": symbols,
        "sensitivity": SENSITIVITY,
        "note": (
            "Uses check_velocity_breakout() as-is: pivot breakout (18-bar high/low) + "
            "volume > 20-bar MA (bar-range proxy on index). Max 1 trade/day. "
            "Premium exit: NIFTY 60/18, BN 150/45, SENSEX 120/35 pts at 0.5 delta. Not financial advice."
        ),
        "results": {
            code: {
                "days_scanned": s.days_scanned,
                "trades": len(s.trades),
                "wins": s.wins,
                "losses": s.losses,
                "win_rate_pct": round(s.win_rate, 1),
                "total_premium_pts": s.total_premium_pts,
                "inr_1_lot": s.inr_pnl(),
                "avg_premium_pts": round(s.total_premium_pts / len(s.trades), 2) if s.trades else 0.0,
                "signal_bars_ce": s.signal_bars_ce,
                "signal_bars_pe": s.signal_bars_pe,
                "sample_trades": [
                    {
                        "day": t.day,
                        "side": t.side,
                        "entry": t.entry_spot,
                        "exit": t.exit_spot,
                        "pnl_premium_pts": t.pnl_premium_pts,
                        "reason": t.exit_reason,
                        "signal": t.signal_type,
                    }
                    for t in s.trades[-5:]
                ],
            }
            for code, s in summaries.items()
        },
    }
