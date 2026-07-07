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
    compute_blr_levels,
    parse_v3_intraday_candles,
)
from app.services.upstox_engine import UpstoxClient, build_upstox_client  # noqa: E402


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
    print(f"NSE day OHLC open : {day_open if day_open is not None else '—'}")
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
    print(f"9:15 5m candle open: {candle_open if candle_open is not None else '—'}")
    if day_open is not None and candle_open is not None:
        print(f"day vs candle diff : {day_open - candle_open:+.2f} pts")
    print()

    prev = _prev_day_ohlc(client, key)
    if prev is None:
        print("Previous day OHLC unavailable — cannot compute BLR preview")
        return
    print(f"Prev close: {prev['close']:.2f}")
    for label, src, opening in (
        ("day_ohlc (live engine)", "day_ohlc", day_open),
        ("5m candle (old engine)", "candle", candle_open),
        ("LTP", "ltp", ltp),
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


def _prev_day_ohlc(client: UpstoxClient, instrument_key: str) -> dict[str, float] | None:
    from datetime import date, timedelta

    today = datetime.now(IST).date()
    to_date = today - timedelta(days=1)
    from_date = today - timedelta(days=14)
    enc = quote(instrument_key, safe="")
    v3_base = client.base_url.replace("/v2", "/v3")
    data = client._get(f"{v3_base}/historical-candle/{enc}/days/1/{to_date}/{from_date}")  # noqa: SLF001
    if not isinstance(data, dict):
        return None
    rows = data.get("candles") or []
    best: dict[str, float] | None = None
    best_day: date | None = None
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        row_day = date.fromisoformat(str(row[0])[:10])
        if row_day >= today:
            continue
        parsed = {
            "date": row_day.isoformat(),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        }
        if best_day is None or row_day > best_day:
            best_day = row_day
            best = parsed
    return best


if __name__ == "__main__":
    main()
