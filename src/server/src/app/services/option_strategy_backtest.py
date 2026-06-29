"""Backtest harness for ``option_strategy.generate_option_buying_signals``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services.backtest_data import HistoricalDataClient, parse_candle_ts
from app.services.backtest_runner import _bar_close_ts, _round_strike
from app.services.option_strategy import generate_option_buying_signals
from app.services.upstox_engine import INDEX_CONFIGS

IST = ZoneInfo("Asia/Kolkata")
SESSION_START = time(9, 15)
SIGNAL_TIME = time(9, 20)
ENTRY_START = time(9, 25)
ENTRY_END = time(14, 45)
SQUARE_OFF = time(15, 25)
DELTA = 0.5
VOLUME_MA_PERIOD = 20


def _effective_volume(candle: dict[str, float]) -> float:
    """Index spot candles often report volume=0; use bar range as activity proxy."""
    vol = float(candle.get("volume") or 0)
    if vol > 0:
        return vol
    high = float(candle.get("high") or candle.get("close") or 0)
    low = float(candle.get("low") or candle.get("close") or 0)
    close = float(candle.get("close") or 0)
    return max((high - low) * close, 1.0)


def _avg_volume(candles: list[dict[str, float]], end_idx: int, period: int = VOLUME_MA_PERIOD) -> float | None:
    """20-bar average volume ending before ``end_idx`` (exclusive)."""
    if end_idx < period:
        return None
    window = candles[end_idx - period : end_idx]
    vols = [_effective_volume(c) for c in window]
    if not vols:
        return None
    return sum(vols) / len(vols)


def _volume_spike(candles: list[dict[str, float]], bar_idx: int) -> bool:
    """Match live rule: entry candle volume above its 20-period average."""
    avg = _avg_volume(candles, bar_idx)
    if avg is None or avg <= 0:
        return False
    return _effective_volume(candles[bar_idx]) > avg


@dataclass
class OptionBacktestTrade:
    symbol: str
    day: str
    side: str  # CE or PE
    entry_spot: float
    exit_spot: float
    pnl_premium_pts: float
    exit_reason: str
    strategy_mode: str
    atm_strike: float | None = None


@dataclass
class OptionBacktestSummary:
    symbol: str
    trades: list[OptionBacktestTrade] = field(default_factory=list)
    days_scanned: int = 0
    signal_days_ce: int = 0
    signal_days_pe: int = 0
    wait_days: int = 0

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


def _pcr_proxy(prev_ohlc: dict[str, float]) -> float:
    """Prior-day direction as a PCR stand-in (no historical OI chain in backtest)."""
    if prev_ohlc["close"] > prev_ohlc["open"] * 1.002:
        return 1.18
    if prev_ohlc["close"] < prev_ohlc["open"] * 0.998:
        return 0.78
    return 1.0


def _premium_risk(symbol: str, atm_strike: float) -> tuple[float, float]:
    if "BANKNIFTY" in symbol:
        return 150.0, 45.0
    if "NIFTY" in symbol:
        return 60.0, 18.0
    if "SENSEX" in symbol:
        return 120.0, 35.0
    return round(atm_strike * 0.015, 1), round(atm_strike * 0.005, 1)


def _spot_thresholds(premium_target: float, premium_sl: float) -> tuple[float, float]:
    return premium_target / DELTA, premium_sl / DELTA


def _simulate_day(
    symbol: str,
    day: date,
    session: list[dict[str, float]],
    prev_ohlc: dict[str, float],
    prior_bars: list[dict[str, float]],
) -> OptionBacktestTrade | None:
    if len(session) < 3:
        return None

    cfg = INDEX_CONFIGS[symbol]
    open_candle = session[0]
    open_ltp = float(open_candle["open"])
    resistance = float(_round_strike(prev_ohlc["high"], cfg.strike_step))
    support = float(_round_strike(prev_ohlc["low"], cfg.strike_step))
    if resistance <= support:
        resistance = float(_round_strike(open_ltp + cfg.strike_step * 2, cfg.strike_step))
        support = float(_round_strike(open_ltp - cfg.strike_step * 2, cfg.strike_step))

    analysis = {
        "symbol": symbol,
        "ltp": open_ltp,
        "pcr": _pcr_proxy(prev_ohlc),
        "resistance_level": resistance,
        "support_level": support,
    }
    signal = generate_option_buying_signals(analysis)
    mode = signal.get("strategy_mode", "")
    setup = signal.get("trade_setup")

    if not setup:
        return None

    action = str(setup.get("Action", ""))
    if "CALL" in action.upper() or "CE" in action.upper():
        side = "CE"
        trigger = resistance
    elif "PUT" in action.upper() or "PE" in action.upper():
        side = "PE"
        trigger = support
    else:
        return None

    atm_strike = float(setup.get("Contract Strike") or _round_strike(open_ltp, cfg.strike_step))
    premium_target, premium_sl = _premium_risk(symbol, atm_strike)
    target_move, sl_move = _spot_thresholds(premium_target, premium_sl)

    combined = prior_bars + session
    prior_offset = len(prior_bars)

    entry_spot: float | None = None
    entry_time: datetime | None = None

    for i, candle in enumerate(session):
        bar_close = _bar_close_ts(candle)
        if bar_close.time() < ENTRY_START or bar_close.time() > ENTRY_END:
            continue
        close = float(candle["close"])
        global_idx = prior_offset + i
        if side == "CE" and close > trigger and _volume_spike(combined, global_idx):
            entry_spot = close
            entry_time = bar_close
            break
        if side == "PE" and close < trigger and _volume_spike(combined, global_idx):
            entry_spot = close
            entry_time = bar_close
            break
    if entry_spot is None or entry_time is None:
        return None

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
            favorable_high = high - entry_spot
            adverse_low = entry_spot - low
            if adverse_low >= sl_move:
                exit_spot = entry_spot - sl_move
                exit_reason = "PREMIUM_SL"
                break
            if favorable_high >= target_move:
                exit_spot = entry_spot + target_move
                exit_reason = "PREMIUM_TARGET"
                break
        else:
            favorable_low = entry_spot - low
            adverse_high = high - entry_spot
            if adverse_high >= sl_move:
                exit_spot = entry_spot + sl_move
                exit_reason = "PREMIUM_SL"
                break
            if favorable_low >= target_move:
                exit_spot = entry_spot - target_move
                exit_reason = "PREMIUM_TARGET"
                break

        if bar_close.time() >= SQUARE_OFF:
            exit_spot = close
            exit_reason = "EOD"
            break
        exit_spot = close

    if side == "CE":
        pnl_premium = (exit_spot - entry_spot) * DELTA
    else:
        pnl_premium = (entry_spot - exit_spot) * DELTA

    return OptionBacktestTrade(
        symbol=symbol,
        day=day.isoformat(),
        side=side,
        entry_spot=round(entry_spot, 2),
        exit_spot=round(exit_spot, 2),
        pnl_premium_pts=round(pnl_premium, 2),
        exit_reason=exit_reason,
        strategy_mode=mode,
        atm_strike=atm_strike,
    )


def run_option_strategy_backtest(
    start: date,
    end: date,
    symbols: list[str] | None = None,
    username: str = "AK07",
) -> dict[str, Any]:
    symbols = symbols or ["NIFTY", "BANKNIFTY", "SENSEX"]
    client = HistoricalDataClient(username=username)
    fetch_start = start - timedelta(days=14)

    candles_by_symbol: dict[str, list[dict[str, float]]] = {}
    daily_by_symbol: dict[str, list[dict[str, float]]] = {}
    for code in symbols:
        cfg = INDEX_CONFIGS[code]
        candles_by_symbol[code] = client.fetch_5m(cfg.spot_instrument_key, fetch_start, end, use_cache=True)
        daily_by_symbol[code] = client.fetch_daily(cfg.spot_instrument_key, fetch_start, end, use_cache=True)

    summaries: dict[str, OptionBacktestSummary] = {s: OptionBacktestSummary(symbol=s) for s in symbols}
    day = start
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        for code in symbols:
            summary = summaries[code]
            summary.days_scanned += 1
            session = _session_candles(candles_by_symbol[code], day)
            prev_ohlc = _prev_day_ohlc(daily_by_symbol[code], day)
            if not session or not prev_ohlc:
                continue

            cfg = INDEX_CONFIGS[code]
            open_ltp = float(session[0]["open"])
            resistance = float(_round_strike(prev_ohlc["high"], cfg.strike_step))
            support = float(_round_strike(prev_ohlc["low"], cfg.strike_step))
            sig = generate_option_buying_signals(
                {
                    "symbol": code,
                    "ltp": open_ltp,
                    "pcr": _pcr_proxy(prev_ohlc),
                    "resistance_level": resistance,
                    "support_level": support,
                }
            )
            mode = sig.get("strategy_mode", "")
            if "CALL" in mode:
                summary.signal_days_ce += 1
            elif "PUT" in mode or "BREAKDOWN" in mode:
                summary.signal_days_pe += 1
            else:
                summary.wait_days += 1

            trade = _simulate_day(
                code,
                day,
                session,
                prev_ohlc,
                _prior_session_bars(candles_by_symbol[code], day, VOLUME_MA_PERIOD),
            )
            if trade:
                summary.trades.append(trade)
        day += timedelta(days=1)

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": symbols,
        "note": (
            "Proxy backtest: prior-day high/low as OI walls, prior-day direction as PCR, "
            "entry requires 5m activity > 20-bar average (bar-range proxy when index volume=0), "
            "SENSEX targets 120/35 premium pts, premium P&L ≈ 0.5× spot move. Not financial advice."
        ),
        "results": {
            code: {
                "days_scanned": s.days_scanned,
                "signal_days_ce": s.signal_days_ce,
                "signal_days_pe": s.signal_days_pe,
                "wait_days": s.wait_days,
                "trades": len(s.trades),
                "wins": s.wins,
                "losses": s.losses,
                "win_rate_pct": round(s.win_rate, 1),
                "total_premium_pts": s.total_premium_pts,
                "inr_1_lot": s.inr_pnl(),
                "avg_premium_pts": round(s.total_premium_pts / len(s.trades), 2) if s.trades else 0.0,
                "sample_trades": [
                    {
                        "day": t.day,
                        "side": t.side,
                        "entry": t.entry_spot,
                        "exit": t.exit_spot,
                        "pnl_premium_pts": t.pnl_premium_pts,
                        "reason": t.exit_reason,
                        "mode": t.strategy_mode,
                    }
                    for t in s.trades[-5:]
                ],
            }
            for code, s in summaries.items()
        },
    }


def _prev_day_ohlc(daily: list[dict[str, float]], day: date) -> dict[str, float] | None:
    prev: dict[str, float] | None = None
    for candle in daily:
        ts = parse_candle_ts(str(candle["timestamp"]))
        if ts.date() < day:
            prev = {
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
            }
        elif ts.date() >= day:
            break
    return prev


def _prior_session_bars(candles: list[dict[str, float]], day: date, count: int) -> list[dict[str, float]]:
    """Last ``count`` completed 5m bars strictly before ``day`` session open."""
    prior: list[dict[str, float]] = []
    for candle in candles:
        ts = parse_candle_ts(str(candle["timestamp"]))
        if ts.date() >= day:
            break
        if ts.weekday() < 5:
            prior.append(candle)
    return prior[-count:]
