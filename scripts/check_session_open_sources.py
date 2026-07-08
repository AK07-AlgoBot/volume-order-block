#!/usr/bin/env python3
"""Compare Upstox session-open sources for S3 BLR parity with TradingView.

Run on server:

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/check_session_open_sources.py [AK07]
"""

from __future__ import annotations

import sys
from datetime import datetime
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


def main() -> None:
    username = (sys.argv[1] if len(sys.argv) > 1 else "AK07").strip()
    client = build_upstox_client(username)
    if client is None:
        print(f"No Upstox client for {username}")
        sys.exit(1)

    cfg = INDEX_CONFIGS["NIFTY"]
    key = cfg.spot_instrument_key
    now = datetime.now(IST)
    print(f"User: {username}  Index: {cfg.display}  {now.strftime('%Y-%m-%d %H:%M:%S')} IST")
    print(f"Instrument: {key}")
    print()

    day_open = client.get_index_day_open(key)
    ltp = client.get_ltp(key)
    print(f"NSE day OHLC open : {day_open if day_open is not None else '—'}  (NOT used — TV uses 5m open)")
    print(f"LTP               : {ltp if ltp is not None else '—'}")

    v3_base = client.base_url.replace("/v2", "/v3")
    enc = quote(key, safe="")
    data = client._get(f"{v3_base}/historical-candle/intraday/{enc}/minutes/{CANDLE_5M}")  # noqa: SLF001
    candles = parse_v3_intraday_candles(data, now) or []
    candle_open = None
    for candle in candles:
        ts = datetime.fromisoformat(candle["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        if ts.date() == now.date() and ts.time() == SESSION_START:
            candle_open = float(candle["open"])
            break
    print(f"9:15 5m candle open: {candle_open if candle_open is not None else '—'}  (engine + TV)")
    if day_open is not None and candle_open is not None:
        print(f"day vs candle diff : {day_open - candle_open:+.2f} pts")
    print()

    market = BreakoutMarketClient()
    prev = market.get_previous_day_ohlc(cfg)
    if prev is None:
        print("Previous day OHLC unavailable — cannot compute BLR preview")
        return
    prev_src = prev.get("prev_close_source", "daily")
    print(f"Prev close: {prev['close']:.2f} ({prev_src})")
    print()
    for label, opening in (
        ("5m candle (live engine)", candle_open),
        ("day_ohlc (ignored)", day_open),
    ):
        if opening is None:
            continue
        levels = compute_blr_levels(
            prev["open"], prev["high"], prev["low"], prev["close"], float(opening), cfg.code
        )
        print(
            f"BLR [{label}] G {levels.green:.2f} / M {levels.mid:.2f} / R {levels.red:.2f} "
            f"(open {float(opening):.2f})"
        )


if __name__ == "__main__":
    main()
