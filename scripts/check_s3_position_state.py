#!/usr/bin/env python3
"""Full S3 position state — Redis + live traders (run on server api container).

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/check_s3_position_state.py
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
from app.services.breakout_order_fanout import list_live_s3_traders, missing_s3_traders  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    index_code = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").strip().upper()
    today = datetime.now(IST).date().isoformat()

    state_key = cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code)
    session_key = cache_manager.BREAKOUT_SESSION_KEY_TEMPLATE.format(day=today, index=index_code)

    raw = cache_manager.get_json(state_key) or {}
    session = cache_manager.get_json(session_key) or {}

    print(f"=== S3 state {index_code} ({today} IST) ===\n")
    print(f"live_traders: {[f'{t.username}@{t.broker}' for t in list_live_s3_traders()]}\n")

    pos_state = raw.get("position") if isinstance(raw, dict) else None
    pos_session = session.get("position") if isinstance(session, dict) else None

    print("--- ak07:breakout_state (dashboard) ---")
    if isinstance(pos_state, dict):
        _print_position(pos_state)
    else:
        print("  position: FLAT")
        print(f"  trades_today: {raw.get('trades_today', '?')}")
        print(f"  spot: {raw.get('spot')}")

    print("\n--- ak07:breakout_session (engine restore) ---")
    if isinstance(pos_session, dict):
        _print_position(pos_session)
    else:
        print("  position: FLAT")
    print(f"  trades_today: {session.get('trades_today', '?')}")

    legs = []
    if isinstance(pos_state, dict):
        legs = pos_state.get("order_legs") or []
    elif isinstance(pos_session, dict):
        legs = pos_session.get("order_legs") or []

    missing = missing_s3_traders(
        legs if isinstance(legs, list) else [],
        assume_upstox_filled=not legs,
    )
    print(f"\n--- fan-out ---")
    print(f"  legs: {[leg.get('username') for leg in legs if isinstance(leg, dict)]}")
    print(f"  missing traders: {[f'{t.username}@{t.broker}' for t in missing]}")

    signals = session.get("signal_log") or raw.get("signals") or []
    if signals:
        print("\n--- recent signals ---")
        for line in signals[-5:]:
            print(f"  {line}")

    if not isinstance(pos_state, dict) and not isinstance(pos_session, dict):
        print(
            "\nEngine shows FLAT — catch-up cannot run. "
            "Check Upstox app if AK07 still holds NIFTY FUT; square off manually if needed."
        )
    elif missing:
        print(f"\nCatch-up possible: python scripts/place_s3_catchup.py {missing[0].username}")

    return 0


def _print_position(pos: dict) -> None:
    print(f"  direction: {pos.get('direction')}")
    print(f"  entry_price: {pos.get('entry_price')}")
    print(f"  sl: {pos.get('sl_price')}  tp1: {pos.get('tp1_price')}")
    print(f"  contract: {pos.get('contract_label')}")
    legs = pos.get("order_legs") or []
    if legs:
        print(f"  order_legs: {json.dumps(legs, indent=4)}")


if __name__ == "__main__":
    raise SystemExit(main())
