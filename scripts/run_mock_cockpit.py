"""One-command mock boot for the AK07 cockpit.

Launches the Streamlit dashboard with AK07_MOCK=1:
- cache_manager swaps Redis for an in-process fakeredis (no server needed),
- mock_data seeds a live-feeling simulated feed on every refresh,
- no Upstox or Telegram credentials are required or contacted.

Usage:  python scripts/run_mock_cockpit.py [--port 8501]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "src" / "server" / "src" / "app" / "ui" / "dashboard.py"


def main() -> int:
    parser = argparse.ArgumentParser(description="Boot the AK07 cockpit with simulated data")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()

    env = dict(os.environ)
    env["AK07_MOCK"] = "1"

    print(f"Booting AK07 cockpit in MOCK mode at http://localhost:{args.port} ...")
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(DASHBOARD),
            "--server.port",
            str(args.port),
        ],
        env=env,
        cwd=str(REPO_ROOT),
    )


if __name__ == "__main__":
    raise SystemExit(main())
