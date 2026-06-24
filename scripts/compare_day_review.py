"""Compare S3 Day Review filter: S2 on/off, S8 with/without (single data load)."""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import date, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src", "server", "src"))

from app.services.backtest_data import HistoricalDataClient
from app.services.backtest_runner import (
    INDEX_CONFIGS,
    BacktestReport,
    S8_ENTRY_START,
    S8_LABEL,
    S8_MAX_TRADES,
    S8_NO_ENTRY,
    S8_SQUARE_OFF,
    SimPosition,
    StructureState,
    _bar_close_ts,
    _exit_on_bar,
    _finalize_trade,
    backtest_strategy_2,
    build_blr_context,
    detect_bos_trend,
    detect_choch,
    s8_adx,
    s8_atr,
    s8_htf_trend,
    update_structure,
)
from app.services.engine_intraday import blr_day_review_allows_direction

START = date(2023, 6, 23)
END = date(2026, 6, 22)
SL_BUF = 0.25


def load_data():
    data = HistoricalDataClient()
    fetch_start = START - timedelta(days=14)
    candles, daily, days = {}, {}, set()
    for code in INDEX_CONFIGS:
        cfg = INDEX_CONFIGS[code]
        candles[code] = data.fetch_5m(cfg.spot_instrument_key, fetch_start, END, use_cache=True)
        daily[code] = data.fetch_daily(cfg.spot_instrument_key, fetch_start, END, use_cache=True)
        days.update(HistoricalDataClient.trading_days(candles[code], START, END))
    return candles, daily, sorted(d for d in days if START <= d <= END)


def stats(report: BacktestReport, strat: str) -> str:
    trades = [t for t in report.trades if t.strategy == strat]
    if not trades:
        return "0 trades"
    wins = sum(1 for t in trades if t.pnl_points > 0)
    pts = sum(t.pnl_points for t in trades)
    return f"{len(trades)} trades | WR {100*wins/len(trades):.1f}% | {pts:+.1f} pts"


def run_s2(candles, daily, days, *, use_day_review: bool) -> BacktestReport:
    report = BacktestReport(start=START, end=END)
    for day in days:
        for code in ("NIFTY", "BANKNIFTY", "SENSEX"):
            session = HistoricalDataClient.session_5m(candles[code], day)
            prev = HistoricalDataClient.prior_session_ohlc(daily[code], day)
            if not session or not prev:
                continue
            blr = build_blr_context(prev, session, code)
            if not blr:
                continue
            if not use_day_review:
                blr = replace(blr, day_review="NEUTRAL")
            backtest_strategy_2(report, day, session, blr, index_code=code)
    return report


def run_s8(candles, daily, days, *, use_day_review: bool, use_prev_day: bool) -> BacktestReport:
    report = BacktestReport(start=START, end=END)
    for day in days:
        for code in INDEX_CONFIGS:
            session = HistoricalDataClient.session_5m(candles[code], day)
            prev = HistoricalDataClient.prior_session_ohlc(daily[code], day)
            if len(session) < 6 or not prev:
                continue
            blr = build_blr_context(prev, session, code)
            if not blr:
                continue
            prev_bias = None
            if use_prev_day:
                if prev["close"] > prev["open"]:
                    prev_bias = "LONG"
                elif prev["close"] < prev["open"]:
                    prev_bias = "SHORT"
            state = StructureState()
            pos, trades_today = None, 0
            for idx, candle in enumerate(session):
                bar_close = _bar_close_ts(candle)
                closed = session[: idx + 1]
                if pos:
                    hit = _exit_on_bar(pos, candle, square_off=S8_SQUARE_OFF, bar_close=bar_close)
                    if hit:
                        _finalize_trade(
                            report, strategy=S8_LABEL, symbol=code, pos=pos,
                            exit_price=hit[0], exit_reason=hit[1], exit_at=bar_close,
                        )
                        pos = None
                    continue
                update_structure(state, closed)
                t = bar_close.time()
                if not (S8_ENTRY_START <= t <= S8_NO_ENTRY) or trades_today >= S8_MAX_TRADES:
                    continue
                direction, lvl = detect_choch(state, closed)
                stype = "CHOCH+BOS"
                if direction is None:
                    direction, lvl = detect_bos_trend(state, closed)
                    stype = "BOS_TREND"
                if direction is None:
                    continue
                adx = s8_adx(closed)
                if adx is None or adx < 20:
                    continue
                htf = s8_htf_trend(closed, bar_close)
                if htf and ((direction == "LONG" and htf == "BEAR") or (direction == "SHORT" and htf == "BULL")):
                    continue
                if prev_bias and direction != prev_bias:
                    continue
                if use_day_review and not blr_day_review_allows_direction(blr.day_review, direction):
                    continue
                atr = s8_atr(closed)
                if atr is None:
                    continue
                entry = float(candle["close"])
                buf = atr * SL_BUF
                if stype == "BOS_TREND":
                    if direction == "LONG":
                        sl = (state.last_sl or lvl * 0.998) - buf
                    else:
                        sl = (state.last_sh or lvl * 1.002) + buf
                else:
                    sl = (lvl - buf) if direction == "LONG" else (lvl + buf)
                if (direction == "LONG" and sl >= entry) or (direction == "SHORT" and sl <= entry):
                    continue
                risk = abs(entry - sl)
                if risk < 1.0:
                    continue
                tp1 = entry + risk * 2 if direction == "LONG" else entry - risk * 2
                trades_today += 1
                pos = SimPosition(
                    direction=direction,
                    entry_price=entry,
                    sl_price=sl,
                    tp1_price=tp1,
                    entry_at=bar_close.isoformat(),
                    entry_reason=stype,
                )
            if pos:
                last = session[-1]
                _finalize_trade(
                    report, strategy=S8_LABEL, symbol=code, pos=pos,
                    exit_price=float(last["close"]), exit_reason="SESSION_END",
                    exit_at=_bar_close_ts(last),
                )
    return report


def main() -> None:
    print("Loading data once...")
    candles, daily, days = load_data()
    print(f"Days: {len(days)}")

    scenarios = [
        ("S2 day-review ON (current)", lambda: run_s2(candles, daily, days, use_day_review=True)),
        ("S2 day-review OFF", lambda: run_s2(candles, daily, days, use_day_review=False)),
        ("S8 prev-day ON, day-review OFF (current)", lambda: run_s8(candles, daily, days, use_day_review=False, use_prev_day=True)),
        ("S8 prev-day ON + day-review ON", lambda: run_s8(candles, daily, days, use_day_review=True, use_prev_day=True)),
        ("S8 day-review ON only", lambda: run_s8(candles, daily, days, use_day_review=True, use_prev_day=False)),
    ]
    for label, fn in scenarios:
        r = fn()
        print(f"\n{label}")
        print(f"  S2: {stats(r, 'Strategy 2 — SMC+CRT')}")
        print(f"  S8: {stats(r, 'Strategy 8 — CHOCH')}")


if __name__ == "__main__":
    main()
