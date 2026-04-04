"""
Per-user trading scope: which symbols the bot should fetch and trade for the session.

Stored at src/server/data/users/<username>/trading_preferences.json (gitignored with user dir).

enabled_scripts: null → trade all symbols from TRADING_CONFIG.
enabled_scripts: ["NIFTY", "CRUDE"] → only those (plus any symbol with an open position is always included by the bot).
"""

from __future__ import annotations

import json
from pathlib import Path

from trading_script_constants import AVAILABLE_SCRIPT_NAMES

from upstox_credentials_store import sanitize_username, user_data_dir

_ALLOWED = frozenset(AVAILABLE_SCRIPT_NAMES)


def preferences_path(username: str) -> Path:
    return user_data_dir(sanitize_username(username)) / "trading_preferences.json"


def read_trading_preferences(username: str) -> dict:
    default: dict = {"enabled_scripts": None}
    path = preferences_path(username)
    if not path.exists():
        return dict(default)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    if not isinstance(raw, dict):
        return dict(default)
    ens = raw.get("enabled_scripts")
    if ens is None:
        return {"enabled_scripts": None}
    if not isinstance(ens, list):
        return dict(default)
    out: list[str] = []
    for x in ens:
        s = str(x).strip().upper()
        if s in _ALLOWED and s not in out:
            out.append(s)
    # Empty list after sanitization → treat as "all" (avoid accidental trade-nothing)
    if not out:
        return {"enabled_scripts": None}
    return {"enabled_scripts": out}


def write_trading_preferences(username: str, enabled_scripts: list[str] | None) -> None:
    path = preferences_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    if enabled_scripts is None:
        payload = {"enabled_scripts": None}
    else:
        out: list[str] = []
        for x in enabled_scripts:
            s = str(x).strip().upper()
            if s in _ALLOWED and s not in out:
                out.append(s)
        if not out:
            raise ValueError("enabled_scripts must be null (all) or a non-empty list of valid symbols")
        payload = {"enabled_scripts": out}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
