#!/usr/bin/env python3
"""Copy legacy performance archives into the persistent data volume.

Old builds wrote to src/server/src/app/archive (ephemeral container layer).
New builds use src/server/data/archive (Docker volume ak07_server_data).

Run on EC2 inside any ak07 container:
  python scripts/migrate_performance_archives.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

from app.services.performance_store import migrate_legacy_archives_to_volume  # noqa: E402


def main() -> None:
    result = migrate_legacy_archives_to_volume(ingest_redis=True)
    print(f"Legacy dir : {result['legacy_dir']}")
    print(f"Target dir : {result['target_dir']}")
    if result["copied"]:
        for name in result["copied"]:
            print(f"copied: {name}")
    else:
        print("No legacy files to copy.")
    if result["ingested_days"]:
        print(f"Ingested Redis for: {', '.join(result['ingested_days'])}")
    print(f"Done — {len(result['copied'])} file(s) copied")


if __name__ == "__main__":
    main()
