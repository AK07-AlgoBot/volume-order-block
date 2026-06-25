"""Persist gamma expiry paper hero signals for post-market P&L analysis."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.config.paths import server_root

logger = logging.getLogger("ak07.gamma_signal_log")

IST: Final = ZoneInfo("Asia/Kolkata")
SIGNAL_DIR: Final[Path] = server_root() / "data" / "logs" / "gamma_paper_signals"


def log_gamma_paper_signal(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Append one JSON line per paper hero alert; return the row."""
    now = now or datetime.now(IST)
    record = {"logged_at": now.isoformat(), **row}

    day = now.date().isoformat()
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = SIGNAL_DIR / f"{day}.jsonl"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Could not write gamma paper log %s: %s", path, exc)

    logger.info(
        "[%s] GAMMA PAPER %s %s%d @ prem %.2f → TP %.2f SL %.2f",
        record.get("index", "?"),
        record.get("signal", "?"),
        record.get("option_type", ""),
        int(record.get("strike") or 0),
        float(record.get("entry_premium") or 0),
        float(record.get("tp_premium") or 0),
        float(record.get("sl_premium") or 0),
    )
    return record
