#!/usr/bin/env python3
"""
Local smoke test: FastAPI health, login, Kite OAuth start config, Zerodha credential test,
and market instruments search. Loads src/server/.env for JWT login (AK07_PASSWORD).

Usage (repo root):
  python scripts/verify_kite_dashboard_api.py
  python scripts/verify_kite_dashboard_api.py --base http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests", file=sys.stderr)
    sys.exit(1)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_server_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = _repo_root() / "src" / "server" / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="API base URL")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    _load_server_dotenv()

    results: list[tuple[str, str, int, str]] = []

    def check(name: str, method: str, path: str, **kw) -> None:
        url = f"{base}{path}"
        try:
            r = requests.request(method, url, timeout=45, **kw)
            snippet = (r.text or "")[:120].replace("\n", " ")
            results.append((name, path, r.status_code, snippet))
        except requests.RequestException as e:
            results.append((name, path, -1, str(e)[:120]))

    # Public
    check("health", "GET", "/api/health")

    # Login
    pwd = (os.environ.get("AK07_PASSWORD") or os.environ.get("DASHBOARD_PASSWORD") or "").strip()
    user = (os.environ.get("DASHBOARD_USER") or "AK07").strip()
    token = ""
    if pwd:
        r = requests.post(
            f"{base}/api/auth/login",
            json={"username": user, "password": pwd},
            timeout=30,
        )
        results.append(("login", "/api/auth/login", r.status_code, (r.text or "")[:80]))
        if r.status_code == 200:
            try:
                token = (r.json() or {}).get("access_token") or ""
            except (ValueError, TypeError):
                pass
    else:
        results.append(
            (
                "login",
                "/api/auth/login",
                -1,
                "skipped (set AK07_PASSWORD or DASHBOARD_PASSWORD in env or src/server/.env)",
            )
        )

    headers = {"Authorization": f"Bearer {token}"} if token else {}

    openapi_paths: set[str] = set()
    try:
        r_open = requests.get(f"{base}/openapi.json", timeout=15)
        if r_open.status_code == 200:
            data = r_open.json()
            openapi_paths = set((data or {}).get("paths") or {})
    except (requests.RequestException, ValueError, TypeError):
        pass

    if token:
        check("me", "GET", "/api/auth/me", headers=headers)
        check(
            "kite_oauth_start",
            "GET",
            "/api/auth/kite/oauth/start",
            headers=headers,
        )
        check(
            "zerodha_credential_test",
            "POST",
            "/api/settings/credentials/test?broker=zerodha",
            headers=headers,
        )
        inst_path = "/api/market/instruments/search"
        if inst_path in openapi_paths:
            check(
                "instruments_search",
                "GET",
                f"{inst_path}?q=REL&limit=5",
                headers=headers,
            )
        else:
            results.append(
                (
                    "instruments_search",
                    inst_path,
                    -1,
                    "skipped (not registered on this server — restart uvicorn after pulling latest code)",
                )
            )

    print("--- Kite / dashboard API checks ---")
    print(f"Base: {base}\n")
    for name, path, code, snippet in results:
        ok = (
            "OK"
            if code == 200
            else ("SKIP" if code == -1 and "skipped" in snippet.lower() else "FAIL")
        )
        print(f"[{ok}] {name:22} {code:4} {path}")
        if code not in (200, -1) or "FAIL" == ok:
            print(f"       -> {snippet}")

    if token:
        print("\nNotes:")
        print("- kite_oauth_start: 503 means KITE_API_KEY / KITE_API_SECRET missing on server (OAuth URL).")
        print("- zerodha_credential_test: 200 means Kite token works through the dashboard.")
        print("- instruments_search: needs valid Zerodha credentials saved for AK07 (only probed if route exists).")

    if not token:
        print("\nTo test authenticated routes, ensure AK07_PASSWORD is in src/server/.env or export it.")

    def _ok(row: tuple[str, str, int, str]) -> bool:
        code, snippet = row[2], row[3]
        if code == 200:
            return True
        if code == -1 and "skipped" in snippet:
            return True
        return False

    failed = [x for x in results if not _ok(x)]
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
