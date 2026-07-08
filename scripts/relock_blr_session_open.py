#!/usr/bin/env python3
"""Re-lock S3 BLR with a TV-confirmed 9:15 session open (first regular-session tick).

Upstox candle/day OHLC open uses the NSE auction price; TradingView uses the first
trade at 9:15. Use this when the engine locked on the wrong open or first-tick capture
was missed.

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/relock_blr_session_open.py 24243.50

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/relock_blr_session_open.py 24243.50 NIFTY
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services import cache_manager  # noqa: E402
from app.services.breakout_engine import (  # noqa: E402
    CANDLE_5M,
    INDEX_CONFIGS,
    IST,
    SESSION_START,
    BreakoutMarketClient,
    compute_blr_levels,
    day_review_from_first_close,
    parse_v3_intraday_candles,
)
from app.services.engine_intraday import blr_day_review_allows_direction  # noqa: E402
from app.services.upstox_engine import build_upstox_client  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: relock_blr_session_open.py SESSION_OPEN [NIFTY|BANKNIFTY|SENSEX]")
        sys.exit(1)
    try:
        session_open = float(sys.argv[1])
    except ValueError:
        print(f"Invalid session open: {sys.argv[1]!r}")
        sys.exit(1)
    index_code = (sys.argv[2] if len(sys.argv) > 2 else "NIFTY").strip().upper()
    cfg = INDEX_CONFIGS.get(index_code)
    if cfg is None:
        print(f"Unknown index: {index_code}")
        sys.exit(1)

    now = datetime.now(IST)
    day = now.date().isoformat()
    market = BreakoutMarketClient()
    prev = market.get_previous_day_ohlc(cfg)
    if prev is None:
        print("Previous day OHLC unavailable")
        sys.exit(1)

    levels = compute_blr_levels(
        prev["open"],
        prev["high"],
        prev["low"],
        prev["close"],
        session_open,
        cfg.code,
    )

    first_close: float | None = None
    client = build_upstox_client("AK07")
    if client is not None:
        v3_base = client.base_url.replace("/v2", "/v3")
        from urllib.parse import quote

        enc = quote(cfg.spot_instrument_key, safe="")
        data = client._get(  # noqa: SLF001
            f"{v3_base}/historical-candle/intraday/{enc}/minutes/{CANDLE_5M}"
        )
        candles = parse_v3_intraday_candles(data, now) or []
        for candle in candles:
            ts = datetime.fromisoformat(candle["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.date() == now.date() and ts.time() == SESSION_START:
                first_close = float(candle["close"])
                break

    day_review = (
        day_review_from_first_close(first_close, levels.mid)
        if first_close is not None
        else "PENDING"
    )

    frozen = {
        "mid": levels.mid,
        "green": levels.green,
        "red": levels.red,
        "gap_regime": levels.gap_regime,
        "band_half": levels.band_half,
        "band_half_pct": levels.band_half_pct,
        "session_open": levels.mid,
        "broker_session_open": session_open,
        "session_open_tv_offset": 0.0,
        "session_open_source": "first_ltp",
        "prev_close": prev["close"],
        "day_review": day_review,
        "first_candle_close": first_close,
    }
    cache_manager.set_json(
        cache_manager.BREAKOUT_FROZEN_KEY_TEMPLATE.format(day=day, index=cfg.code),
        frozen,
        ttl_seconds=86_400 * 2,
    )
    cache_manager.set_json(
        cache_manager.BREAKOUT_OPEN_TICK_KEY_TEMPLATE.format(day=day, index=cfg.code),
        {
            "price": session_open,
            "captured_at": now.isoformat(),
            "source": "manual_tv",
        },
        ttl_seconds=86_400 * 2,
    )

    state_key = cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=cfg.code)
    state = cache_manager.get_json(state_key) or {}
    if not isinstance(state, dict):
        state = {}
    state.update(
        {
            "index": cfg.code,
            "display": cfg.display,
            "mid": levels.mid,
            "green": levels.green,
            "red": levels.red,
            "gap_regime": levels.gap_regime,
            "band_half": levels.band_half,
            "band_half_pct": levels.band_half_pct,
            "session_open": levels.mid,
            "broker_session_open": session_open,
            "session_open_tv_offset": 0.0,
            "session_open_source": "first_ltp",
            "prev_close": prev["close"],
            "levels_ready": True,
            "day_review": day_review,
            "first_candle_close": first_close,
            "allowed_long": blr_day_review_allows_direction(day_review, "LONG"),
            "allowed_short": blr_day_review_allows_direction(day_review, "SHORT"),
            "setup_label": (
                f"BLR re-locked (TV tick) — G {levels.green:.2f} / M {levels.mid:.2f} / "
                f"R {levels.red:.2f} ({levels.gap_regime} · review {day_review})"
            ),
            "updated_at": now.isoformat(),
        }
    )
    cache_manager.set_json(state_key, state, ttl_seconds=cache_manager.LIVE_STATE_TTL_SECONDS)

    print(f"Index: {cfg.display}  Day: {day}")
    print(f"Session open (TV tick): {session_open:.2f}")
    print(f"Prev close: {prev['close']:.2f}")
    print(
        f"BLR: G {levels.green:.2f} / M {levels.mid:.2f} / R {levels.red:.2f} "
        f"({levels.gap_regime})"
    )
    if first_close is not None:
        print(f"Day review: {day_review} (1st 5m close {first_close:.2f} vs mid {levels.mid:.2f})")
    print()
    print("Restart breakout_engine to load in-memory state:")
    print("  docker compose -p ak07 -f configs/docker-compose.yml restart breakout_engine")


if __name__ == "__main__":
    main()
