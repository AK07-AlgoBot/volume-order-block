"""Per-user broker HTTP egress (SEBI static IP).

Users without ``egress_ip`` use the host default outbound IP.
Users with ``egress_ip`` send broker API calls via a host-side CONNECT
proxy bound to that IP (Docker bridge cannot bind host secondary IPs).

Env:
  AK07_EGRESS_PROXY=http://172.19.0.1:18901
      Default proxy when egress_ip is set but not listed in the map.
  AK07_EGRESS_PROXY_MAP=65.109.255.239=http://172.19.0.1:18901,95.216.179.8=http://172.19.0.1:18902
      Per-IP proxy URLs (required when you have more than one secondary IP).
  AK07_EGRESS_IPS=Kesavulu:65.109.255.239
      Optional username→IP override of profile.egress_ip.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("ak07.broker_http")


def _parse_egress_ips_env() -> dict[str, str]:
    raw = (os.environ.get("AK07_EGRESS_IPS") or "").strip()
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        user, ip = part.split(":", 1)
        user, ip = user.strip(), ip.strip()
        if user and ip:
            out[user] = ip
    return out


def _parse_egress_proxy_map() -> dict[str, str]:
    """Parse AK07_EGRESS_PROXY_MAP=ip=url,ip=url."""
    raw = (os.environ.get("AK07_EGRESS_PROXY_MAP") or "").strip()
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        ip, url = part.split("=", 1)
        ip, url = ip.strip(), url.strip()
        if ip and url:
            out[ip] = url
    return out


def resolve_egress_ip(username: str) -> str:
    """Return dedicated egress IP for username, or '' for default host IP."""
    safe = (username or "").strip()
    if not safe:
        return ""
    env_map = _parse_egress_ips_env()
    if safe in env_map:
        return env_map[safe]
    try:
        from app.services.user_profiles_store import read_profile

        return str(read_profile(safe).get("egress_ip") or "").strip()
    except Exception as exc:
        logger.debug("egress_ip lookup failed for %s: %s", safe, exc)
        return ""


def resolve_egress_proxy(username: str) -> str:
    """HTTP CONNECT proxy URL for the user's dedicated egress IP."""
    ip = resolve_egress_ip(username)
    if not ip:
        return ""
    mapped = _parse_egress_proxy_map().get(ip, "").strip()
    if mapped:
        return mapped
    return (os.environ.get("AK07_EGRESS_PROXY") or "").strip()


class _SourceAddressAdapter(HTTPAdapter):
    """Bind outbound sockets to a host IP (needs network_mode:host / bare metal)."""

    def __init__(self, source_ip: str, **kwargs: Any) -> None:
        self._source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["source_address"] = (self._source_ip, 0)
        return super().init_poolmanager(*args, **kwargs)


def session_for_user(username: str) -> requests.Session:
    """requests.Session that exits via the user's SEBI static IP when configured."""
    session = requests.Session()
    proxy = resolve_egress_proxy(username)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        logger.info("[%s] broker HTTP via egress proxy %s", username, proxy)
        return session

    ip = resolve_egress_ip(username)
    if ip:
        adapter = _SourceAddressAdapter(ip)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        logger.info("[%s] broker HTTP source bind %s", username, ip)
    return session


def post_for_user(username: str, url: str, **kwargs: Any) -> requests.Response:
    return session_for_user(username).post(url, **kwargs)


def get_for_user(username: str, url: str, **kwargs: Any) -> requests.Response:
    return session_for_user(username).get(url, **kwargs)
