#!/usr/bin/env python3
"""Check dashboard users and live broker token status (Upstox / Kite / Groww).

Run on server:

  cd ~/volume-order-block
  docker compose -p ak07 -f configs/docker-compose.yml exec -T api python scripts/check_broker_tokens.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services.user_profiles_store import read_profile  # noqa: E402
from app.services.users_store import list_users  # noqa: E402
from groww_credentials_store import read_credentials_file_for_user as groww_creds  # noqa: E402
from kite_credentials_store import read_credentials_file_for_user as kite_creds  # noqa: E402
from upstox_credentials_store import read_credentials_file_for_user  # noqa: E402


def _test_upstox(c: dict[str, str]) -> str:
    token = (c.get("access_token") or "").strip()
    if not token:
        return "no token saved"
    base = (c.get("base_url") or "https://api.upstox.com/v2").rstrip("/")
    try:
        resp = requests.get(
            f"{base}/user/profile",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    except requests.RequestException as exc:
        return f"error: {exc}"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}"
    data = resp.json().get("data") or {}
    return f"OK — {data.get('user_name')} {data.get('user_id')}"


def _test_kite(c: dict[str, str]) -> str:
    token = (c.get("access_token") or "").strip()
    api_key = (c.get("api_key") or "").strip()
    if not token:
        return "no token saved"
    if not api_key:
        return "missing api_key"
    base = (c.get("base_url") or "https://api.kite.trade").rstrip("/")
    try:
        resp = requests.get(
            f"{base}/user/profile",
            headers={"Authorization": f"token {api_key}:{token}"},
            timeout=20,
        )
    except requests.RequestException as exc:
        return f"error: {exc}"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}"
    data = resp.json().get("data") or {}
    return f"OK — {data.get('user_name')} {data.get('user_id')}"


def _test_groww(c: dict[str, str]) -> str:
    token = (c.get("access_token") or "").strip()
    if not token:
        return "no token saved"
    base = (c.get("base_url") or "https://api.groww.in").rstrip("/")
    try:
        from app.services.groww_token import fetch_user_profile

        data = fetch_user_profile(access_token=token, base_url=base)
    except RuntimeError as exc:
        return str(exc)[:120]
    except requests.RequestException as exc:
        return f"error: {exc}"
    ucc = data.get("ucc") or data.get("vendor_user_id") or "?"
    segments = data.get("active_segments") or data.get("segments") or []
    seg_note = f" segments={segments}" if segments else ""
    return f"OK — UCC {ucc}{seg_note}"


def main() -> int:
    print("=== AK07 broker token status (all dashboard users) ===\n")
    users = list_users()
    if not users:
        print("No users in users_auth.json")
        return 1

    for row in users:
        username = row["username"]
        role = row.get("role", "?")
        prof = read_profile(username, role=role)
        broker = str(prof.get("broker") or "upstox")
        paper = bool(prof.get("paper_trading"))
        strategies = prof.get("enabled_strategies") or []
        print(f"--- {username} ---")
        print(f"  role={role}  broker={broker}  paper={paper}")
        print(f"  strategies={strategies}")
        for label, read_fn, test_fn in (
            ("upstox", read_credentials_file_for_user, _test_upstox),
            ("kite", kite_creds, _test_kite),
            ("groww", groww_creds, _test_groww),
        ):
            creds = read_fn(username)
            on_disk = bool((creds.get("access_token") or "").strip())
            live = test_fn(creds) if on_disk else "no token saved"
            marker = "CONNECTED" if live.startswith("OK") else "NOT CONNECTED"
            print(f"  {label:6} on_disk={on_disk}  {marker} — {live}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
