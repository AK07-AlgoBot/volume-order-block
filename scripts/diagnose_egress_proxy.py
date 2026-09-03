#!/usr/bin/env python3
"""Diagnose SEBI egress proxy issues (502 Bad Gateway on broker APIs).

Run on the Docker *host* (or inside api container with network access):

  python3 scripts/diagnose_egress_proxy.py
  python3 scripts/diagnose_egress_proxy.py --user Kesavulu
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "lib"))
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

import requests  # noqa: E402

from broker_http import resolve_egress_ip, resolve_egress_proxy, session_for_user  # noqa: E402


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL {msg}")


def _check_ip_on_host(ip: str) -> bool:
    if not ip:
        return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((ip, 0))
        return True
    except OSError as exc:
        _fail(f"secondary IP {ip} not bindable on this host: {exc}")
        return False


def _check_proxy_listen(proxy_url: str) -> bool:
    if not proxy_url:
        return True
    # http://172.19.0.1:18901 -> host, port
    raw = proxy_url.replace("http://", "").replace("https://", "")
    host, _, port_s = raw.partition(":")
    try:
        port = int(port_s or "80")
    except ValueError:
        _fail(f"invalid proxy URL {proxy_url}")
        return False
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
        _ok(f"proxy reachable at {host}:{port}")
        return True
    except OSError as exc:
        _fail(f"cannot connect to proxy {proxy_url}: {exc}")
        return False


def _check_proxy_tunnel(proxy_url: str, bind_hint: str) -> bool:
    if not proxy_url:
        return True
    try:
        resp = requests.get(
            "https://api.kite.trade/",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=15,
            allow_redirects=False,
        )
        _ok(f"CONNECT tunnel via proxy works (HTTP {resp.status_code})")
        return True
    except requests.RequestException as exc:
        _fail(f"CONNECT tunnel via {proxy_url} failed: {exc}")
        if "502" in str(exc) and bind_hint:
            print(
                f"       → proxy is up but outbound bind to {bind_hint} failed.\n"
                f"         Run: ip -4 addr | grep {bind_hint}\n"
                f"         Fix: systemctl restart ak07-egress-proxy (or re-add IP on eth0)"
            )
        return False


def _check_user_broker(username: str) -> None:
    print(f"\n=== User {username!r} ===")
    ip = resolve_egress_ip(username)
    proxy = resolve_egress_proxy(username)
    print(f"  egress_ip: {ip or '(primary — no proxy)'}")
    print(f"  proxy:     {proxy or '(none)'}")
    if not ip:
        _ok("uses primary host IP — no egress proxy needed")
        return
    _check_ip_on_host(ip)
    _check_proxy_listen(proxy)
    _check_proxy_tunnel(proxy, ip)
    try:
        from app.services.broker_connection_status import broker_connection_status  # noqa: PLC0415

        status = broker_connection_status(username)
        if status.get("connected"):
            _ok(f"broker {status.get('broker')}: {status.get('detail')}")
        else:
            _fail(f"broker {status.get('broker')}: {status.get('detail')}")
    except Exception as exc:
        _fail(f"broker_connection_status failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose AK07 SEBI egress proxy")
    parser.add_argument("--user", help="Check one username (default: env + all egress users)")
    args = parser.parse_args()

    print("=== Environment ===")
    print(f"  AK07_EGRESS_PROXY={os.environ.get('AK07_EGRESS_PROXY', '') or '(unset)'}")
    print(f"  AK07_EGRESS_PROXY_MAP={os.environ.get('AK07_EGRESS_PROXY_MAP', '') or '(unset)'}")
    print(f"  AK07_EGRESS_IPS={os.environ.get('AK07_EGRESS_IPS', '') or '(unset)'}")

    proxy_default = (os.environ.get("AK07_EGRESS_PROXY") or "").strip()
    proxy_map_raw = (os.environ.get("AK07_EGRESS_PROXY_MAP") or "").strip()
    if proxy_default:
        _check_proxy_listen(proxy_default)
    for part in proxy_map_raw.split(","):
        part = part.strip()
        if "=" in part:
            _, url = part.split("=", 1)
            _check_proxy_listen(url.strip())

    if args.user:
        _check_user_broker(args.user)
        return 0

    # Scan user profiles for egress_ip
    users_dir = REPO / "src" / "server" / "data" / "users"
    found = False
    if users_dir.is_dir():
        for prof in sorted(users_dir.glob("*/profile.json")):
            try:
                import json

                data = json.loads(prof.read_text(encoding="utf-8"))
            except Exception:
                continue
            ip = str(data.get("egress_ip") or "").strip()
            if ip:
                found = True
                _check_user_broker(prof.parent.name)
    if not found:
        print("\nNo users with egress_ip in profile.json (check AK07_EGRESS_IPS env).")
        env_ips = (os.environ.get("AK07_EGRESS_IPS") or "").strip()
        if env_ips:
            for part in env_ips.split(","):
                if ":" in part:
                    user = part.split(":", 1)[0].strip()
                    if user:
                        _check_user_broker(user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
