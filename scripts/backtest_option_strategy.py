#!/usr/bin/env python3
"""3-year backtest for option_strategy.py (ATM CE/PE buying on OI wall breaks).

  cd volume-order-block
  set PYTHONPATH=src\\server\\src
  python scripts/backtest_option_strategy.py
  python scripts/backtest_option_strategy.py --json-out backtest_option_3y.json
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

os.environ["AK07_MOCK"] = "0"
os.environ["AK07_MOCK_MODE"] = "0"
os.environ["AK07_PAPER_TRADING"] = "1"

from app.services.option_strategy_backtest import run_option_strategy_backtest  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def main() -> None:
    parser = argparse.ArgumentParser(description="Option buying strategy backtest (NIFTY/BANKNIFTY/SENSEX)")
    parser.add_argument("--from", dest="from_date", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--years", type=float, default=3.0, help="Lookback years if --from omitted")
    parser.add_argument("--json-out", default=str(REPO / "backtest_option_3y.json"))
    args = parser.parse_args()

    end = date.fromisoformat(args.to_date) if args.to_date else datetime.now(IST).date()
    if args.from_date:
        start = date.fromisoformat(args.from_date)
    else:
        start = end - timedelta(days=int(args.years * 365.25))

    print("=" * 72)
    print("Option Buying Strategy Backtest (option_strategy.py)")
    print(f"Period: {start} -> {end}  (~{args.years}y)")
    print("Indices: NIFTY, BANKNIFTY, SENSEX")
    print("=" * 72)

    report = run_option_strategy_backtest(start, end)

    for code in ("NIFTY", "BANKNIFTY", "SENSEX"):
        r = report["results"][code]
        print(f"\n--- {code} ---")
        print(f"  Trading days scanned : {r['days_scanned']}")
        print(f"  CE signal days       : {r['signal_days_ce']}")
        print(f"  PE signal days       : {r['signal_days_pe']}")
        print(f"  Wait / no-setup days : {r['wait_days']}")
        print(f"  Trades taken         : {r['trades']}")
        print(f"  Win rate             : {r['win_rate_pct']}%  ({r['wins']}W / {r['losses']}L)")
        print(f"  Total premium pts    : {r['total_premium_pts']:+.2f}")
        print(f"  Avg premium pts/trade: {r['avg_premium_pts']:+.2f}")
        print(f"  INR (1 lot)          : {r['inr_1_lot']:+,.0f}")

    print(f"\nNote: {report['note']}")

    out = Path(args.json_out)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
