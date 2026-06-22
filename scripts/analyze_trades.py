#!/usr/bin/env python3
"""Print loss post-mortem from Redis + archives (run inside api container on EC2)."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

from app.services import performance_store  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze AK07 closed trades and losses.")
    parser.add_argument("--days", type=int, default=30, help="Look back N calendar days")
    parser.add_argument("--live-only", action="store_true", help="Exclude paper_trading rows")
    args = parser.parse_args()

    end = datetime.now(IST).date()
    start = end - timedelta(days=max(1, args.days) - 1)
    trades = performance_store.load_trades(start_date=start, end_date=end)
    if args.live_only:
        trades = [t for t in trades if not t.get("paper_trading")]

    report = performance_store.analyze_losses(trades)
    print(f"Range: {start} → {end}")
    print(f"Trades: {report['total_trades']}  Wins: {report['wins']}  Losses: {report['losses']}")
    print()
    print(report["filter_note"])
    print()

    if report["by_bucket"]:
        print("Loss causes:")
        for cause, count in sorted(report["by_bucket"].items(), key=lambda x: -x[1]):
            print(f"  {count:3d}  {cause}")
        print()

    if report["by_strategy"]:
        print("By strategy:")
        for row in report["by_strategy"]:
            print(f"  {row['Strategy']}: {row['Losses']} losses ({row['Loss pts']:+.2f} pts)")
        print()

    if report["loss_rows"]:
        print("Losing trades:")
        for row in report["loss_rows"]:
            print(
                f"  {row['date']}  {row['strategy'][:28]:28s}  {row['symbol']} {row['direction']:5s}  "
                f"{row['pnl_pts']:+7.2f}  {row['loss_bucket']}  [{row['exit_reason']}]"
            )


if __name__ == "__main__":
    main()
