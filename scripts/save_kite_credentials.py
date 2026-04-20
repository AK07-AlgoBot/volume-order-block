#!/usr/bin/env python3
"""
Write Zerodha Kite credentials to src/server/data/users/<user>/zerodha_credentials.json
without using the dashboard (fallback when UI/API login is unavailable).

Usage:
  python scripts/save_kite_credentials.py --user AK07 --api-key KEY --access-token TOKEN
  python scripts/save_kite_credentials.py --user AK07 --api-key KEY --api-secret SECRET --access-token TOKEN

Requires: repo root as cwd, or PYTHONPATH including src/lib (script adds it).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "lib"))

from zerodha_credentials_store import (  # noqa: E402
    credentials_file_for_user,
    persist_credentials_for_user,
    read_credentials_file_for_user,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Save Kite credentials JSON for a dashboard user.")
    ap.add_argument("--user", default="AK07", help="Dashboard username (credential folder name)")
    ap.add_argument("--api-key", dest="api_key", default="", help="Kite API key")
    ap.add_argument("--api-secret", dest="api_secret", default="", help="Kite API secret")
    ap.add_argument("--access-token", dest="access_token", default="", help="Kite access_token (session)")
    ap.add_argument("--base-url", dest="base_url", default="https://api.kite.trade", help="REST base URL")
    args = ap.parse_args()

    cur = read_credentials_file_for_user(args.user)
    if args.api_key.strip():
        cur["api_key"] = args.api_key.strip()
    if args.api_secret.strip():
        cur["api_secret"] = args.api_secret.strip()
    if args.access_token.strip():
        cur["access_token"] = args.access_token.strip()
    if args.base_url.strip():
        cur["base_url"] = args.base_url.strip()

    if not (cur.get("access_token") or "").strip():
        raise SystemExit("access_token is required (paste from Kite session or OAuth).")
    if not (cur.get("api_key") or "").strip():
        raise SystemExit("api_key is required.")

    out = persist_credentials_for_user(args.user, cur)
    print(f"Saved credentials for {args.user}: access_token={'yes' if out.get('access_token') else 'no'}")
    print(f"File: {credentials_file_for_user(args.user)}")


if __name__ == "__main__":
    main()
