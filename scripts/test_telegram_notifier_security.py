#!/usr/bin/env python3
"""Self-check: secret redaction + Markdown fallback for Telegram alerts.

  python scripts/test_telegram_notifier_security.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

from app.services.telegram_notifier import (  # noqa: E402
    _deliver_text,
    _escape_markdown,
    redact_secrets,
)


def _selfcheck_redact() -> None:
    leaked = "token 123456789:AAHfake_telegram_bot_token_xx access_token=abc.def.ghi"
    out = redact_secrets(leaked)
    assert "123456789:" not in out, out
    assert "[REDACTED]" in out, out
    assert "access_token=[REDACTED]" in out, out
    assert redact_secrets("NIFTY CE 24500") == "NIFTY CE 24500"
    print("redact self-check ok")


def _selfcheck_escape() -> None:
    raw = "NSE_FO|NIFTY25SEP24500CE"
    escaped = _escape_markdown(raw)
    assert "_" not in escaped.replace("\\_", ""), escaped
    assert "\\_" in escaped
    print("escape self-check ok")


def _selfcheck_plain_fallback() -> None:
    client = MagicMock()
    bad = MagicMock()
    bad.status_code = 400
    bad.text = '{"description":"can\'t parse entities"}'
    good = MagicMock()
    good.status_code = 200
    client.post.side_effect = [bad, good]

    monkey_creds = ("000:placeholder", "-1")
    import app.services.telegram_notifier as tn

    original = tn._credentials
    tn._credentials = lambda: monkey_creds
    try:
        _deliver_text(client, "hello *world* NSE_FO|x")
    finally:
        tn._credentials = original

    assert client.post.call_count == 2
    first = client.post.call_args_list[0].kwargs["json"]
    second = client.post.call_args_list[1].kwargs["json"]
    assert first.get("parse_mode") == "Markdown"
    assert "parse_mode" not in second
    print("plain-text fallback self-check ok")


def main() -> None:
    _selfcheck_redact()
    _selfcheck_escape()
    _selfcheck_plain_fallback()
    print("telegram notifier security self-check passed")


if __name__ == "__main__":
    main()
