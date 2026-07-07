#!/usr/bin/env python3
"""Verify Groww NIFTY future resolution for a dashboard user (e.g. Nani).

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \
    python scripts/check_groww_s3_future.py Nani
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services.breakout_order_fanout import list_live_s3_traders  # noqa: E402
from app.services.groww_engine import GrowwClient  # noqa: E402
from app.services.user_profiles_store import read_profile  # noqa: E402
from app.services.users_store import get_user_record  # noqa: E402


def main() -> int:
    username = (sys.argv[1] if len(sys.argv) > 1 else "Nani").strip()
    rec = get_user_record(username)
    if not rec:
        print(f"User {username!r} not found")
        return 1
    profile = read_profile(username, role=rec["role"])
    print(f"=== Groww S3 future check — {username} ===")
    print(f"  broker={profile.get('broker')}  paper={profile.get('paper_trading')}")
    print(f"  strategies={profile.get('enabled_strategies')}")
    traders = list_live_s3_traders()
    print(f"  live_s3_traders={[f'{t.username}@{t.broker}' for t in traders]}")

    client = GrowwClient(username)
    if not client.has_token():
        print("NOT READY — no Groww access_token")
        return 1
    contract = client.get_index_future_contract("NIFTY")
    if not contract:
        print("FAILED — could not resolve NIFTY future on Groww")
        return 1
    print("\nGroww would trade:")
    for key, val in contract.items():
        print(f"  {key}: {val}")
    print("\nOK — Groww NIFTY future resolved for live S3 orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
