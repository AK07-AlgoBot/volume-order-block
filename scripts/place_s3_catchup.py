#!/usr/bin/env python3
"""Place missing S3 fan-out legs for an open breakout position (e.g. Nani on Groww).

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/place_s3_catchup.py Nani
"""

from __future__ import annotations

import sys
from pathlib import Path

from datetime import datetime
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

IST = ZoneInfo("Asia/Kolkata")

from app.services import cache_manager  # noqa: E402
from app.services.breakout_order_fanout import (  # noqa: E402
    legs_summary,
    list_live_s3_traders,
    missing_s3_traders,
    place_s3_entries,
)
from app.services.breakout_engine import INDEX_CONFIGS, LOTS_PER_TRADE  # noqa: E402
from app.services.upstox_engine import build_upstox_client  # noqa: E402


def main() -> int:
    username = (sys.argv[1] if len(sys.argv) > 1 else "Nani").strip()
    index_code = (sys.argv[2] if len(sys.argv) > 2 else "NIFTY").strip().upper()

    raw = cache_manager.get_json(cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code))
    if not isinstance(raw, dict):
        raw = {}

    today = datetime.now(IST).date().isoformat()
    pos = raw.get("position")
    if not isinstance(pos, dict):
        session = cache_manager.get_json(
            cache_manager.BREAKOUT_SESSION_KEY_TEMPLATE.format(day=today, index=index_code)
        )
        if isinstance(session, dict) and isinstance(session.get("position"), dict):
            pos = session["position"]
            print("Note: breakout_state flat — using session restore position.")
        else:
            print(f"No open S3 position on {index_code} — nothing to catch up.")
            print(f"  trades_today (state): {raw.get('trades_today', '?')}")
            if isinstance(session, dict):
                print(f"  trades_today (session): {session.get('trades_today', '?')}")
            print("Run: python scripts/check_s3_position_state.py")
            return 1

    direction = str(pos.get("direction") or "")
    legs = pos.get("order_legs") if isinstance(pos.get("order_legs"), list) else []
    missing = missing_s3_traders(legs, assume_upstox_filled=not legs)
    targets = [t for t in missing if t.username == username]
    if not targets:
        print(f"{username} is not in missing traders list.")
        print(f"  live traders: {[f'{t.username}@{t.broker}' for t in list_live_s3_traders()]}")
        print(f"  covered legs: {[leg.get('username') for leg in legs if isinstance(leg, dict)]}")
        return 0

    print(f"Open position: {direction} @ {pos.get('entry_price')} — catching up {username}")
    cfg = INDEX_CONFIGS[index_code]
    upstox = build_upstox_client("AK07")
    new_legs = place_s3_entries(
        index_code=index_code,
        direction=direction,
        lot_size=cfg.lot_size,
        lots=LOTS_PER_TRADE,
        upstox_market_client=upstox,
        global_paper=False,
        only_usernames=frozenset({username}),
    )
    placed = new_legs
    if not placed:
        print(f"FAILED — no order placed for {username}. Check Groww token and breakout logs.")
        return 1

    merged = list(legs) + placed
    pos["order_legs"] = merged
    raw["position"] = pos
    cache_manager.set_json(cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code), raw, ttl_seconds=86_400)

    print(f"OK — {legs_summary(placed)}")
    for leg in placed:
        oid = leg.get("groww_order_id") or leg.get("upstox_order_id") or "?"
        print(f"  order_id={oid}  qty={leg.get('quantity')}  sym={leg.get('trading_symbol')}")
    print("\nRedis breakout state updated with new order leg.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
