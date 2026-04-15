#!/usr/bin/env python3
"""
Send a one-off Telegram message to the configured group (same token/chat as trading_bot).

Usage (repo root):
  python scripts/telegram_announce.py
  python scripts/telegram_announce.py "Your *Markdown* text here"

Docker (from repo root on host):
  docker compose -f configs/docker-compose.yml exec bot python -u scripts/telegram_announce.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "src" / "bot", _REPO / "src" / "lib"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from trading_bot import send_telegram_test_message  # noqa: E402

DEFAULT_MESSAGE = (
    "📢 *Options integration*\n\n"
    "Companion options flow is enabled for index futures (NIFTY / BANKNIFTY / SENSEX) per "
    "`options_*` settings in `TRADING_CONFIG` — ladder GTT / exits as configured.\n\n"
    "_AK07 stack_"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a Telegram announcement via bot settings.")
    parser.add_argument(
        "message",
        nargs="?",
        default=DEFAULT_MESSAGE,
        help="Message body (Telegram Markdown).",
    )
    args = parser.parse_args()
    ok = send_telegram_test_message(args.message)
    print("Sent." if ok else "Failed (see logs / HTTP error).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
