#!/usr/bin/env python3
"""Manually trigger the Upstox V3 daily token request (same path as the engine).

Usage (repo root / inside api or engine container):

  python scripts/refresh_upstox_token.py
  docker compose -p ak07 -f configs/docker-compose.yml exec engine python scripts/refresh_upstox_token.py

Exits 0 when a bearer token is present after the request; exits 1 on hard failure.
Note: API-only Upstox apps may return UDAPI1123 until a valid notifier URL is
configured in the Upstox developer portal — paste today's token manually if so.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.services.upstox_engine import UpstoxClient  # noqa: E402


def main() -> int:
    client = UpstoxClient()
    ok = client.request_daily_access_token()
    token = (client.session.headers.get("Authorization") or "").replace("Bearer ", "")
    print(f"request_daily_access_token returned: {ok}")
    print(f"access_token length: {len(token)}")
    if not ok or not token:
        print("Token refresh did not succeed. Check engine logs / Upstox notifier URL.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
