#!/usr/bin/env python3
"""Copy open S3 position from breakout_session → breakout_state (dashboard).

Use when breakout_state shows FLAT but session still has an open position
(e.g. after a restart desync). Then restart breakout_engine.

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/resync_s3_position_from_session.py NIFTY

  docker compose -p ak07 -f configs/docker-compose.yml restart breakout_engine
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services import cache_manager  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    index_code = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").strip().upper()
    today = datetime.now(IST).date().isoformat()

    state_key = cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code)
    session_key = cache_manager.BREAKOUT_SESSION_KEY_TEMPLATE.format(day=today, index=index_code)

    session = cache_manager.get_json(session_key) or {}
    raw = cache_manager.get_json(state_key) or {}
    if not isinstance(session, dict):
        session = {}
    if not isinstance(raw, dict):
        raw = {}

    pos = session.get("position")
    if not isinstance(pos, dict):
        print(f"No open position in session key for {index_code} ({today}).")
        return 1

    raw["position"] = pos
    raw["trades_today"] = int(session.get("trades_today") or raw.get("trades_today") or 0)
    signals = session.get("signal_log")
    if isinstance(signals, list) and signals:
        raw["signals"] = signals[-10:]

    cache_manager.set_json(state_key, raw, ttl_seconds=86_400)

    print(f"OK — synced session position → breakout_state for {index_code}")
    print(f"  direction: {pos.get('direction')}  entry: {pos.get('entry_price')}")
    print(f"  sl: {pos.get('sl_price')}  tp1: {pos.get('tp1_price')}  tp2: {pos.get('tp2_price')}")
    print(f"  legs: {[leg.get('username') for leg in (pos.get('order_legs') or []) if isinstance(leg, dict)]}")
    print("\nRestart breakout_engine so the live process reloads the position:")
    print("  docker compose -p ak07 -f configs/docker-compose.yml restart breakout_engine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
