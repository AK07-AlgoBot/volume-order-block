#!/usr/bin/env python3
"""Run AK07 strategy backtests on historical Upstox data (local, not mock mode).

Requires a valid Upstox access token:
  src/server/data/users/AK07/upstox_credentials.json

Examples:
  python scripts/backtest_ak07.py --days 20
  python scripts/backtest_ak07.py --from 2026-05-01 --to 2026-06-10 --strategies s1,s3
  python scripts/backtest_ak07.py --days 30 --indices NIFTY --json-out backtest_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

# Historical replay only — never use mock candles.
os.environ["AK07_MOCK"] = "0"
os.environ["AK07_MOCK_MODE"] = "0"
os.environ["AK07_PAPER_TRADING"] = "1"

from app.services.backtest_runner import STRATEGY_RUNNERS, run_backtest  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest AK07 strategies on historical Upstox candles.")
    parser.add_argument("--days", type=int, default=20, help="Calendar days to look back from --to (default 20)")
    parser.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", help="Start date (IST session days)")
    parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD", help="End date (default: today IST)")
    parser.add_argument(
        "--strategies",
        default="all",
        help="Comma list: all | s1,s2,s3,s7 (default all)",
    )
    parser.add_argument(
        "--indices",
        default="NIFTY,BANKNIFTY,SENSEX",
        help="Comma list of index codes (default all three)",
    )
    parser.add_argument("--user", default="AK07", help="Credentials username folder (default AK07)")
    parser.add_argument("--no-cache", action="store_true", help="Refetch candles from Upstox (ignore disk cache)")
    parser.add_argument("--json-out", metavar="PATH", help="Write full trade list + summary JSON to this file")
    parser.add_argument("--verbose-trades", action="store_true", help="Print every closed trade")
    args = parser.parse_args()

    end = _parse_date(args.to_date) if args.to_date else datetime.now(IST).date()
    if args.from_date:
        start = _parse_date(args.from_date)
    else:
        start = end - timedelta(days=max(1, args.days) - 1)

    if args.strategies.strip().lower() == "all":
        strategies = set(STRATEGY_RUNNERS.keys())
    else:
        strategies = {s.strip().lower() for s in args.strategies.split(",") if s.strip()}
        unknown = strategies - set(STRATEGY_RUNNERS.keys())
        if unknown:
            print(f"Unknown strategies: {', '.join(sorted(unknown))}")
            print(f"Valid: {', '.join(sorted(STRATEGY_RUNNERS.keys()))}, all")
            return 2

    indices = {x.strip().upper() for x in args.indices.split(",") if x.strip()}

    print(f"AK07 historical backtest  {start} -> {end}")
    print(f"Strategies: {', '.join(sorted(strategies))}")
    print(f"Indices: {', '.join(sorted(indices))}")
    print(f"User: {args.user}  Cache: {'off' if args.no_cache else 'on'}")
    print()

    try:
        report = run_backtest(
            start=start,
            end=end,
            strategies=strategies,
            indices=indices,
            username=args.user,
            use_cache=not args.no_cache,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    summary = report.summary()

    for note in summary.get("notes", []):
        print(f"NOTE: {note}")
    for skipped in summary.get("skipped_strategies", []):
        print(f"SKIP: {skipped}")
    if summary.get("notes") or summary.get("skipped_strategies"):
        print()

    print(f"Trades: {summary['total_trades']}  Wins: {summary['wins']}  Losses: {summary['losses']}  "
          f"Win rate: {summary['win_rate_pct']}%  Total: {summary['total_pts']:+.2f} pts")
    print()

    if summary["by_strategy"]:
        print("By strategy:")
        for name, row in sorted(summary["by_strategy"].items()):
            print(
                f"  {name}: {int(row['trades'])} trades, "
                f"{int(row['wins'])}W/{int(row['losses'])}L, {float(row['pts']):+.2f} pts"
            )
        print()

    if args.verbose_trades and report.trades:
        print("Trades:")
        for t in report.trades:
            print(
                f"  {t.entry_at[:10]}  {t.strategy[:28]:28s}  {t.symbol} {t.direction:5s}  "
                f"{t.pnl_points:+7.2f}  [{t.exit_reason}]  {t.entry_reason[:50]}"
            )

    if args.json_out:
        out_path = Path(args.json_out)
        payload = {
            "summary": summary,
            "trades": [t.as_dict() for t in report.trades],
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {out_path}")

    return 0 if summary["total_trades"] or summary.get("notes") else 1


if __name__ == "__main__":
    raise SystemExit(main())
