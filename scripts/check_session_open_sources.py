#!/usr/bin/env python3
"""Compare Upstox session-open sources for S3 BLR parity with TradingView.

Run on server:

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/check_session_open_sources.py [AK07] [TV_MID]

Optional TV_MID — your TradingView mid line for gap diagnostics (e.g. 24243.50).
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services.breakout_engine import (  # noqa: E402
    CANDLE_5M,
    INDEX_CONFIGS,
    IST,
    SESSION_START,
    BreakoutMarketClient,
    compute_blr_levels,
    parse_v3_intraday_candles,
)
from app.services.upstox_engine import build_upstox_client  # noqa: E402


def _parse_ts(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def _fetch_intraday(client, instrument_key: str, interval: str, now: datetime) -> list[dict[str, float]]:
    v3_base = client.base_url.replace("/v2", "/v3")
    enc = quote(instrument_key, safe="")
    data = client._get(  # noqa: SLF001
        f"{v3_base}/historical-candle/intraday/{enc}/minutes/{interval}"
    )
    return parse_v3_intraday_candles(data, now) or []


def _candle_at(candles: list[dict[str, float]], day, bar_time: time) -> dict[str, float] | None:
    for candle in candles:
        ts = _parse_ts(candle["timestamp"])
        if ts.date() == day and ts.time() == bar_time:
            return candle
    return None


def _print_blr(label: str, opening: float | None, prev: dict[str, float], code: str) -> None:
    if opening is None:
        return
    levels = compute_blr_levels(
        prev["open"], prev["high"], prev["low"], prev["close"], float(opening), code
    )
    print(
        f"BLR [{label}] G {levels.green:.2f} / M {levels.mid:.2f} / R {levels.red:.2f} "
        f"(open {float(opening):.2f})"
    )


def main() -> None:
    username = (sys.argv[1] if len(sys.argv) > 1 else "AK07").strip()
    tv_mid: float | None = None
    if len(sys.argv) > 2:
        try:
            tv_mid = float(sys.argv[2])
        except ValueError:
            print(f"Invalid TV_MID: {sys.argv[2]!r}")
            sys.exit(1)

    client = build_upstox_client(username)
    if client is None:
        print(f"No Upstox client for {username}")
        sys.exit(1)

    cfg = INDEX_CONFIGS["NIFTY"]
    key = cfg.spot_instrument_key
    now = datetime.now(IST)
    today = now.date()
    print(f"User: {username}  Index: {cfg.display}  {now.strftime('%Y-%m-%d %H:%M:%S')} IST")
    print(f"Instrument: {key}")
    print()

    day_open = client.get_index_day_open(key)
    ltp = client.get_ltp(key)
    print(f"NSE day OHLC open : {day_open if day_open is not None else '—'}")
    print(f"LTP               : {ltp if ltp is not None else '—'}")

    candles_5m = _fetch_intraday(client, key, CANDLE_5M, now)
    bar_915 = _candle_at(candles_5m, today, SESSION_START)
    bar_920 = _candle_at(candles_5m, today, (datetime.combine(today, SESSION_START) + timedelta(minutes=5)).time())

    candle_open = float(bar_915["open"]) if bar_915 else None
    candle_close_915 = float(bar_915["close"]) if bar_915 else None
    candle_open_920 = float(bar_920["open"]) if bar_920 else None

    print(f"9:15 5m candle open : {candle_open if candle_open is not None else '—'}  (engine uses this)")
    if candle_close_915 is not None:
        print(f"9:15 5m candle close: {candle_close_915:.2f}  (first bar close @ ~9:20)")
    if candle_open_920 is not None:
        print(f"9:20 5m candle open : {candle_open_920:.2f}  (≈ prior bar close if no gap)")
    if day_open is not None and candle_open is not None:
        print(f"day vs 9:15 open diff: {day_open - candle_open:+.2f} pts")
    print()

    candles_1m = _fetch_intraday(client, key, "1", now)
    bar_1m_915 = _candle_at(candles_1m, today, SESSION_START)
    if bar_1m_915:
        print(
            f"9:15 1m candle      : O {float(bar_1m_915['open']):.2f} "
            f"H {float(bar_1m_915['high']):.2f} L {float(bar_1m_915['low']):.2f} "
            f"C {float(bar_1m_915['close']):.2f}"
        )
        print()

    fut = client.get_index_future_contract("NIFTY")
    if fut:
        fut_key = str(fut.get("instrument_key") or "")
        fut_open = None
        if fut_key:
            fut_bar = _candle_at(_fetch_intraday(client, fut_key, CANDLE_5M, now), today, SESSION_START)
            if fut_bar:
                fut_open = float(fut_bar["open"])
        print(f"Front-month future  : {fut.get('trading_symbol') or fut_key}")
        print(f"Future 9:15 5m open : {fut_open if fut_open is not None else '—'}")
        print()

    morning = [
        c
        for c in candles_5m
        if _parse_ts(c["timestamp"]).date() == today
        and time(9, 0) <= _parse_ts(c["timestamp"]).time() <= time(10, 0)
    ]
    if morning:
        print("Morning 5m candles (9:00–10:00):")
        for candle in morning:
            ts = _parse_ts(candle["timestamp"])
            print(
                f"  {ts.strftime('%H:%M')}  O {float(candle['open']):8.2f}  "
                f"H {float(candle['high']):8.2f}  L {float(candle['low']):8.2f}  "
                f"C {float(candle['close']):8.2f}"
            )
        print()

    market = BreakoutMarketClient()
    prev = market.get_previous_day_ohlc(cfg)
    if prev is None:
        print("Previous day OHLC unavailable — cannot compute BLR preview")
        return
    prev_src = prev.get("prev_close_source", "daily")
    print(f"Prev close: {prev['close']:.2f} ({prev_src})")
    print()

    _print_blr("9:15 open (engine)", candle_open, prev, cfg.code)
    if candle_close_915 is not None:
        _print_blr("9:15 close (if TV misaligned)", candle_close_915, prev, cfg.code)
    if candle_open_920 is not None:
        _print_blr("9:20 open (2nd bar)", candle_open_920, prev, cfg.code)
    _print_blr("day_ohlc", day_open, prev, cfg.code)

    if tv_mid is not None and candle_open is not None:
        gap = candle_open - tv_mid
        print()
        print(f"TV mid reference    : {tv_mid:.2f}")
        print(f"Engine mid gap      : {gap:+.2f} pts (Upstox 9:15 open − TV mid)")
        if candle_close_915 is not None and abs(candle_close_915 - tv_mid) < abs(gap):
            print(
                f"Hint: TV mid is closer to 9:15 bar CLOSE ({candle_close_915:.2f}) "
                f"than OPEN ({candle_open:.2f}) — check TV Data Window on 9:15 bar."
            )


if __name__ == "__main__":
    main()
