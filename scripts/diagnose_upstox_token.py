#!/usr/bin/env python3
"""Diagnose AK07 Upstox V3 auto-token flow (request + notifier webhook).

Run on EC2 (repo root or inside api/engine container):

  python scripts/diagnose_upstox_token.py
  docker compose -p ak07 -f configs/docker-compose.yml exec api python scripts/diagnose_upstox_token.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from upstox_credentials_store import read_credentials_file_for_user  # noqa: E402

NOTIFIER_PUBLIC = os.environ.get(
    "UPSTOX_NOTIFIER_URL", "https://ak07.in/api/upstox/token-notifier"
)
LOCAL_NOTIFIER = os.environ.get(
    "UPSTOX_NOTIFIER_LOCAL", "http://127.0.0.1:8080/api/upstox/token-notifier"
)
LOCAL_HEALTH = os.environ.get("AK07_API_HEALTH", "http://127.0.0.1:8080/api/health")


def _probe(label: str, url: str) -> None:
    print(f"\n--- {label}: GET {url} ---")
    try:
        r = requests.get(url, timeout=10)
        print(f"HTTP {r.status_code}")
        text = (r.text or "")[:400]
        print(text)
        if r.status_code != 200:
            print("FAIL: expected HTTP 200")
            return
        if "ok" not in text.lower() and "streamlit" in text.lower():
            print("FAIL: response looks like Streamlit — nginx /api/ is NOT proxied to FastAPI")
            return
        print("OK")
    except requests.RequestException as exc:
        print(f"FAIL: {exc}")


def main() -> int:
    creds = read_credentials_file_for_user("AK07")
    client_id = (creds.get("api_key") or "").strip()
    client_secret = (creds.get("api_secret") or "").strip()
    token = (creds.get("access_token") or "").strip()

    print("=== AK07 Upstox token diagnostics ===")
    print(f"api_key present: {bool(client_id)} (len={len(client_id)})")
    print(f"api_secret present: {bool(client_secret)}")
    print(f"access_token on disk: {bool(token)} (len={len(token)})")

    _probe("Local API health", LOCAL_HEALTH)
    _probe("Local notifier probe", LOCAL_NOTIFIER)
    _probe("Public notifier probe (Upstox validates this)", NOTIFIER_PUBLIC)

    if not client_id or not client_secret:
        print("\nSTOP: api_key/api_secret missing in upstox_credentials.json")
        return 1

    print(f"\n--- Token request: POST .../v3/login/auth/token/request/{client_id[:8]}… ---")
    url = f"https://api.upstox.com/v3/login/auth/token/request/{client_id}"
    try:
        r = requests.post(
            url,
            json={"client_secret": client_secret},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=20,
        )
        print(f"HTTP {r.status_code}")
        try:
            body = r.json()
            print(json.dumps(body, indent=2)[:800])
        except ValueError:
            print((r.text or "")[:400])

        if r.status_code == 200:
            data = body.get("data") if isinstance(body, dict) else {}
            notifier = (data or {}).get("notifier_url") if isinstance(data, dict) else None
            print("\nSUCCESS: request accepted.")
            if notifier:
                print(f"Upstox will POST token to: {notifier}")
            print("Next: approve the prompt in Upstox mobile/web/WhatsApp within ~15 min.")
            print("Then check: docker compose ... logs --tail=20 api | grep -i notifier")
            return 0

        errors = body.get("errors") if isinstance(body, dict) else []
        code = ""
        if errors and isinstance(errors[0], dict):
            code = str(errors[0].get("errorCode") or errors[0].get("error_code") or "")
        if code == "UDAPI1123":
            print(
                "\nUDAPI1123 fix checklist:\n"
                "  1. Upstox My Apps → Edit app → Notifier = "
                f"{NOTIFIER_PUBLIC}\n"
                "  2. Public GET must return JSON (not Streamlit HTML)\n"
                "  3. nginx location /api/ → proxy_pass http://127.0.0.1:8080;\n"
                "  4. api container must be running (docker compose ps api)\n"
                "  5. After nginx fix, Edit+Save app in Upstox portal again"
            )
        return 1
    except requests.RequestException as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
