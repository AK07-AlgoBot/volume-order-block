"""Tail-read active orders.log for the dashboard (bounded size)."""

from __future__ import annotations

from pathlib import Path

MAX_TAIL_LINES = 10_000
MAX_READ_BYTES = 1_048_576


def tail_orders_log(path: Path, max_lines: int) -> tuple[list[str], bool]:
    """
    Return up to max_lines non-empty-truncated tail lines and whether output was truncated
    (large file or more lines than max_lines).
    """
    cap = min(max(1, max_lines), MAX_TAIL_LINES)
    if not path.is_file():
        return [], False

    try:
        size = path.stat().st_size
    except OSError:
        return [], False
    if size == 0:
        return [], False

    truncated = False
    read_size = min(size, MAX_READ_BYTES)
    if read_size < size:
        truncated = True

    try:
        with path.open("rb") as f:
            if read_size < size:
                f.seek(size - read_size)
            raw = f.read()
    except OSError:
        return [], False

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > cap:
        lines = lines[-cap:]
        truncated = True
    return lines, truncated
