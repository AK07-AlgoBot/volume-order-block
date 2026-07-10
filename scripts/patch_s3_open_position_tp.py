#!/usr/bin/env python3
"""Recompute TP1/TP2 on the open S3 breakout position (e.g. Friday 1:1 patch).

Updates Redis breakout_state + breakout_session. Restart breakout_engine after
running so the live process reloads the new targets from session.

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/patch_s3_open_position_tp.py NIFTY

  docker compose -p ak07 -f configs/docker-compose.yml restart breakout_engine
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services import cache_manager  # noqa: E402
from app.services.breakout_engine import IST, trade_levels  # noqa: E402

TTL = 86_400 * 2


def _patch_position(pos: dict[str, Any], *, index_code: str, session_date) -> dict[str, Any]:
    direction = str(pos.get("direction") or "")
    entry = float(pos.get("entry_price") or 0)
    if not direction or entry <= 0:
        raise ValueError("position missing direction or entry_price")

    sl, tp1, tp2 = trade_levels(
        index_code,
        direction,
        entry,
        mid=entry,
        green=entry,
        red=entry,
        gap_regime="",
        session_date=session_date,
    )
    updated = dict(pos)
    updated["sl_price"] = sl
    updated["tp1_price"] = tp1
    updated["tp2_price"] = tp2
    return updated


def main() -> int:
    index_code = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").strip().upper()
    today = datetime.now(IST).date()
    session_date = today

    state_key = cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code)
    session_key = cache_manager.BREAKOUT_SESSION_KEY_TEMPLATE.format(day=today.isoformat(), index=index_code)

    raw = cache_manager.get_json(state_key)
    session = cache_manager.get_json(session_key)
    if not isinstance(raw, dict):
        raw = {}
    if not isinstance(session, dict):
        session = {}

    pos = raw.get("position")
    if not isinstance(pos, dict):
        pos = session.get("position") if isinstance(session.get("position"), dict) else None
    if not isinstance(pos, dict):
        print(f"No open S3 position on {index_code}.")
        return 1

    old_tp1 = pos.get("tp1_price")
    old_tp2 = pos.get("tp2_price")
    new_pos = _patch_position(pos, index_code=index_code, session_date=session_date)

    note = (
        f"{index_code} open position TP patched "
        f"TP1 {old_tp1} -> {new_pos['tp1_price']:.2f} "
        f"TP2 {old_tp2} -> {new_pos['tp2_price']:.2f} "
        f"({session_date.strftime('%A')} 1:1 rule)"
    )

    raw["position"] = new_pos
    cache_manager.set_json(state_key, raw, ttl_seconds=86_400)

    session["position"] = new_pos
    logs = session.get("signal_log")
    if not isinstance(logs, list):
        logs = []
    logs.append(note)
    session["signal_log"] = logs[-20:]
    cache_manager.set_json(session_key, session, ttl_seconds=TTL)

    print(f"OK — {note}")
    print(f"  direction: {new_pos.get('direction')}  entry: {new_pos.get('entry_price')}")
    print(f"  sl: {new_pos.get('sl_price')}  tp1: {new_pos.get('tp1_price')}  tp2: {new_pos.get('tp2_price')}")
    print("\nRestart breakout_engine so the running process reloads targets:")
    print("  docker compose -p ak07 -f configs/docker-compose.yml restart breakout_engine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
