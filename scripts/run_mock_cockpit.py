"""One-command mock boot for the AK07 cockpit.

Launches the Streamlit dashboard with AK07_MOCK=1:
- cache_manager swaps Redis for an in-process fakeredis (no server needed),
- mock_data seeds a live-feeling simulated feed on every refresh,
- no Upstox or Telegram credentials are required or contacted.

Optional engine simulation frame (default on) prints tick logs showing the
entry gate transition before Streamlit starts.

Usage:
  python scripts/run_mock_cockpit.py [--port 8501]
  python scripts/run_mock_cockpit.py --no-simulate
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "src" / "server" / "src" / "app" / "ui" / "dashboard.py"
SERVER_SRC = REPO_ROOT / "src" / "server" / "src"


def run_engine_simulation_frame(ticks: int = 5) -> None:
    """Run mock engine ticks; expect 'monitoring active execution boundaries' in logs."""
    os.environ["AK07_MOCK"] = "1"
    sys.path.insert(0, str(SERVER_SRC))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from app.services import cache_manager  # noqa: PLC0415
    from app.services.smc_crt_engine import SMCCRTEngine  # noqa: PLC0415
    from app.services.upstox_engine import AK07Engine  # noqa: PLC0415

    cache_manager.set_system_bias("NEUTRAL")
    engine = AK07Engine()
    smc_engine = SMCCRTEngine()

    print("--- AK07 mock engine simulation frame ---")
    saw_monitoring = False
    for i in range(ticks):
        engine.tick()
        smc_engine.tick()
        nifty = cache_manager.get_json(cache_manager.INDEX_STATE_KEY_TEMPLATE.format(index="NIFTY")) or {}
        if nifty.get("monitoring_active"):
            saw_monitoring = True
            print(f"[simulation] tick {i + 1}: monitoring active execution boundaries (spot={nifty.get('spot')})")
    if not saw_monitoring:
        print("[simulation] warning: monitoring flag not reached; check mock drift ticks")
    print("--- simulation complete ---\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Boot the AK07 cockpit with simulated data")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument(
        "--no-simulate",
        action="store_true",
        help="Skip the mock engine tick simulation frame",
    )
    parser.add_argument("--simulate-ticks", type=int, default=5)
    args = parser.parse_args()

    env = dict(os.environ)
    env["AK07_MOCK"] = "1"
    env["PYTHONPATH"] = str(SERVER_SRC)

    if not args.no_simulate:
        run_engine_simulation_frame(ticks=args.simulate_ticks)

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
