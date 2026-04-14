#!/usr/bin/env python3
"""
Reset the dashboard password for AK07 (local file: src/server/data/users_auth.json).

Usage (from repo root):

  python scripts/reset_dashboard_password.py
  python scripts/reset_dashboard_password.py -p "YourNewPassword"
  set AK07_NEW_PASSWORD=YourNewPassword && python scripts/reset_dashboard_password.py

After reset, restart the FastAPI server so in-memory state is not stale (if any).

Alternative without this script: delete users_auth.json, set AK07_PASSWORD in src/server/.env,
then start the API once — ensure_seeded_users() will create AK07 with that password.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

try:
    import bcrypt
except ImportError:
    print("Install bcrypt: pip install bcrypt", file=sys.stderr)
    sys.exit(1)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _users_auth_path() -> Path:
    return _repo_root() / "src" / "server" / "data" / "users_auth.json"


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset AK07 dashboard password (local users_auth.json).")
    parser.add_argument(
        "-p",
        "--password",
        help="New password (prefer omitting: you will be prompted, or use AK07_NEW_PASSWORD env).",
    )
    args = parser.parse_args()

    pwd = (args.password or os.environ.get("AK07_NEW_PASSWORD") or "").strip()
    if not pwd:
        p1 = getpass.getpass("New password for AK07: ")
        p2 = getpass.getpass("Confirm: ")
        if p1 != p2:
            print("Passwords do not match.", file=sys.stderr)
            return 1
        pwd = p1
    if len(pwd) < 1:
        print("Password cannot be empty.", file=sys.stderr)
        return 1

    path = _users_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "users": [
            {
                "username": "AK07",
                "password_hash": _hash_password(pwd),
                "role": "user",
            }
        ]
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"OK: wrote {path}")
    print("Log in as AK07 with the new password. Restart the API if it is running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
