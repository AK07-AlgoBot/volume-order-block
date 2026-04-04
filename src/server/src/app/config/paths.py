"""Repository layout: root contains `src/` (client, server, bot, lib) and `configs/`."""

from __future__ import annotations

import sys
from pathlib import Path


def repo_root() -> Path:
    """Top of the repository (parent of `src/`)."""
    # This file: repo/src/server/src/app/config/paths.py
    return Path(__file__).resolve().parents[5]


def server_root() -> Path:
    """`src/server` — API package, `data/`, `templates/`."""
    return Path(__file__).resolve().parents[3]


def lib_root() -> Path:
    """Shared Python modules (`upstox_credentials_store`, etc.)."""
    return repo_root() / "src" / "lib"


def bot_root() -> Path:
    return repo_root() / "src" / "bot"


def ensure_repo_and_lib_on_path() -> None:
    """Allow `import upstox_credentials_store` and `import bot_process_control` from the API."""
    r = repo_root()
    lib = lib_root()
    bot = bot_root()
    for p in (r, lib, bot):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
