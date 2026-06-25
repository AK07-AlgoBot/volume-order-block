"""Persist CHOCH signals that fired but failed execution gates (live observer)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.config.paths import server_root

logger = logging.getLogger("ak07.choch_signal_log")

IST: Final = ZoneInfo("Asia/Kolkata")
REJECTED_DIR: Final[Path] = server_root() / "data" / "logs" / "choch_rejected"


def log_rejected_signal(
    *,
    index_code: str,
    signal_type: str,
    direction: str,
    signal_level: float,
    spot: float,
    reason: str,
    structure: str,
    now: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one JSON line per rejected signal; return the row for Redis/UI."""
    now = now or datetime.now(IST)
    row: dict[str, Any] = {
        "at": now.isoformat(),
        "index": index_code,
        "signal_type": signal_type,
        "direction": direction,
        "signal_level": round(signal_level, 2),
        "spot": round(spot, 2),
        "structure": structure,
        "reason": reason,
    }
    if extra:
        row.update(extra)

    day = now.date().isoformat()
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    path = REJECTED_DIR / f"{day}.jsonl"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Could not write CHOCH rejected log %s: %s", path, exc)

    logger.info(
        "[%s] CHOCH REJECTED %s %s @ %.0f — %s",
        index_code,
        signal_type,
        direction,
        signal_level,
        reason,
    )
    return row
