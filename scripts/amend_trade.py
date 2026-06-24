#!/usr/bin/env python3
"""Amend a completed trade in Redis (manual exit / square-off corrections).

Examples (on server, from repo root):
  docker compose -f configs/docker-compose.yml exec api \\
    python scripts/amend_trade.py --day 2026-06-24 --list --strategy s8_choch

  docker compose -f configs/docker-compose.yml exec api \\
    python scripts/amend_trade.py --day 2026-06-24 --strategy s8_choch \\
    --symbol BANKNIFTY --trade-index -1 \\
    --exit-price 58240 --exit-reason INTRADAY_SQUARE_OFF_1455 \\
    --exit-at "2026-06-24T14:55:00+05:30"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

from app.services import performance_store  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def main() -> None:
    parser = argparse.ArgumentParser(description="Amend completed trade(s) in Redis.")
    parser.add_argument("--day", default=datetime.now(IST).date().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--list", action="store_true", help="List trades for the day")
    parser.add_argument("--strategy", default="", help="strategy_id filter (e.g. s8_choch)")
    parser.add_argument("--symbol", default="", help="NIFTY | BANKNIFTY | SENSEX")
    parser.add_argument(
        "--trade-index",
        type=int,
        default=-1,
        help="Index in filtered list (-1 = last match)",
    )
    parser.add_argument("--entry-price", type=float, default=None)
    parser.add_argument("--exit-price", type=float, required=False)
    parser.add_argument("--exit-reason", default=None)
    parser.add_argument("--exit-at", default=None, help='ISO time e.g. "2026-06-24T14:55:00+05:30"')
    args = parser.parse_args()

    sid = args.strategy or None
    sym = args.symbol.upper() if args.symbol else None

    if args.list:
        rows = performance_store.list_completed_trades(args.day, strategy_id=sid)
        if sym:
            rows = [r for r in rows if str(r.get("symbol", "")).upper() == sym]
        if not rows:
            print(f"No trades for {args.day}")
            return
        print(json.dumps(rows, indent=2))
        return

    if args.exit_price is None and args.exit_reason is None and args.exit_at is None:
        parser.error("Provide --exit-price, --exit-reason, and/or --exit-at (or use --list)")

    updated = performance_store.amend_completed_trade(
        args.day,
        strategy_id=sid,
        symbol=sym,
        trade_index=args.trade_index,
        entry_price=args.entry_price,
        exit_price=args.exit_price,
        exit_reason=args.exit_reason,
        exit_at=args.exit_at,
    )
    if not updated:
        print("No matching trade found or Redis update failed.", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(updated, indent=2))


if __name__ == "__main__":
    main()
