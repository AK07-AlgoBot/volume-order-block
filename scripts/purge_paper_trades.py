#!/usr/bin/env python3
"""Remove paper/simulation trades from Redis + session archives.

Use after a session ran without a valid Upstox token (PAPER mode) so
Performance Review shows live accuracy only.

Examples (on EC2, from repo root):
  docker compose -p ak07 -f configs/docker-compose.yml exec api \\
    python scripts/purge_paper_trades.py --day 2026-06-06

  docker compose -p ak07 -f configs/docker-compose.yml exec api \\
    python scripts/purge_paper_trades.py --last-friday --dry-run
"""

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


def _last_friday(today: date | None = None) -> date:
    today = today or datetime.now(IST).date()
    # Monday=0 … Friday=4
    days_since_friday = (today.weekday() - 4) % 7
    if days_since_friday == 0 and today.weekday() != 4:
        days_since_friday = 7
    if today.weekday() == 4:
        return today - timedelta(days=7)
    return today - timedelta(days=days_since_friday)


def main() -> None:
    parser = argparse.ArgumentParser(description="Purge paper/simulation trades for one or more days.")
    parser.add_argument("--day", action="append", help="Session day YYYY-MM-DD (repeatable)")
    parser.add_argument("--last-friday", action="store_true", help="Purge the most recent Friday session")
    parser.add_argument(
        "--all-trades",
        action="store_true",
        help="Remove every trade for the day, not just paper_trading rows",
    )
    parser.add_argument("--keep-archive", action="store_true", help="Do not delete session archive JSON")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    args = parser.parse_args()

    days: list[str] = list(args.day or [])
    if args.last_friday:
        days.append(_last_friday().isoformat())
    if not days:
        parser.error("Specify --day YYYY-MM-DD and/or --last-friday")

    days = sorted(set(days))
    results = performance_store.purge_trades_for_days(
        days,
        paper_only=not args.all_trades,
        remove_archive=not args.keep_archive,
        dry_run=args.dry_run,
    )

    mode = "DRY RUN" if args.dry_run else "DONE"
    print(f"{mode} — purged paper trades for {len(results)} day(s)\n")
    for row in results:
        print(f"  {row['day']}:")
        print(f"    Redis removed : {row['redis_removed']} (kept {row['redis_remaining']})")
        if row["archive_path"]:
            print(f"    Archive       : {row['archive_path']}")
            print(f"    Archive gone  : {row['archive_removed']}")
        else:
            print("    Archive       : (none)")
        print(f"    Trade log gone: {row['trade_log_removed']}")

    print("\nTip: In Performance Review, set Mode → Live only to hide any remaining paper rows.")


if __name__ == "__main__":
    main()
