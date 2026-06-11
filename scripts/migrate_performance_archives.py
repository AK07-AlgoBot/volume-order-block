#!/usr/bin/env python3
"""Copy legacy performance archives into the persistent data volume.

Old builds wrote to src/server/src/app/archive (ephemeral container layer).
New builds use src/server/data/archive (Docker volume ak07_server_data).

Run on EC2 inside any ak07 container:
  python scripts/migrate_performance_archives.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

from app.config.paths import archive_dir  # noqa: E402
from app.services.performance_store import LEGACY_ARCHIVE_DIR, ingest_strategy1_trade_log  # noqa: E402

import json  # noqa: E402


def main() -> None:
    target = archive_dir()
    target.mkdir(parents=True, exist_ok=True)
    if not LEGACY_ARCHIVE_DIR.is_dir():
        print(f"No legacy folder at {LEGACY_ARCHIVE_DIR}")
        return

    moved = 0
    for path in sorted(LEGACY_ARCHIVE_DIR.glob("performance_review_*.json")):
        dest = target / path.name
        if dest.exists():
            print(f"skip (exists): {dest.name}")
            continue
        shutil.copy2(path, dest)
        moved += 1
        print(f"copied: {path.name} -> {dest}")
        try:
            payload = json.loads(dest.read_text(encoding="utf-8"))
            day = str(payload.get("date") or dest.stem.replace("performance_review_", ""))
            trade_log = payload.get("trade_log") or []
            if isinstance(trade_log, list):
                ingest_strategy1_trade_log(
                    day,
                    trade_log,
                    paper_trading=bool(payload.get("paper_trading", True)),
                )
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            print(f"  warning: could not ingest Redis for {path.name}: {exc}")

    print(f"Done — copied {moved} file(s) to {target}")


if __name__ == "__main__":
    main()
