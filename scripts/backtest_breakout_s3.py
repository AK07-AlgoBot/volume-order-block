#!/usr/bin/env python3
"""S3 BLR breakout backtest — Pine v10 aligned (30 SL / 60 TP, 0.211% band).

Default: **no** stop-after-first-TP (keep trading after 1st target hit; up to max/day).
Use --stop-after-first-tp to match live Pine (day done when 1st trade reaches target).

Requires Upstox token: src/server/data/users/AK07/upstox_credentials.json

Examples:
  cd volume-order-block
  python scripts/backtest_breakout_s3.py --years 17 --indices NIFTY
  python scripts/backtest_breakout_s3.py --from 2020-01-01 --to 2026-06-30
  python scripts/backtest_breakout_s3.py --years 3 --stop-after-first-tp --json-out backtest_s3_daydone.json
  python scripts/backtest_breakout_s3.py --years 17 --csv-out trades.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]

# Pine v8 / server trial defaults — must be set before app imports.
os.environ.setdefault("AK07_MOCK", "0")
os.environ.setdefault("AK07_MOCK_MODE", "0")
os.environ.setdefault("AK07_PAPER_TRADING", "1")
os.environ.setdefault("BREAKOUT_SIZING_MODE", "fixed_sl_tp")
os.environ.setdefault("BREAKOUT_FIXED_SL_PTS", "30")
os.environ.setdefault("BREAKOUT_FIXED_TP_PTS", "60")
os.environ.setdefault("BREAKOUT_MAX_TRADES_PER_DAY", "3")
os.environ.setdefault("BREAKOUT_NO_ENTRY_AFTER_IST", "13:00")
os.environ.setdefault("BREAKOUT_SQUARE_OFF_IST", "14:55")
os.environ.setdefault("BREAKOUT_BAND_PCT_NIFTY", "0.211")
os.environ.setdefault("BREAKOUT_BAND_PCT_BANKNIFTY", "0.125")
os.environ.setdefault("BREAKOUT_BAND_PCT_SENSEX", "0.14")
os.environ.setdefault("BREAKOUT_STOP_AFTER_FIRST_TP", "0")

sys.path.insert(0, str(REPO / "src" / "server" / "src"))

from app.services.backtest_runner import run_backtest  # noqa: E402
from app.services import performance_store  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw.strip())


def _exit_breakdown(trades: list) -> dict[str, dict[str, float | int]]:
    buckets: dict[str, dict[str, float | int]] = {}
    for t in trades:
        reason = t.exit_reason
        if "TP1" in reason or reason == "TARGET":
            key = "TP (~60)"
        elif "SL" in reason:
            key = "SL (~30)"
        elif "1455" in reason or "SQUARE" in reason:
            key = "14:55 square-off"
        else:
            key = reason
        b = buckets.setdefault(key, {"count": 0, "pnl": 0.0})
        b["count"] = int(b["count"]) + 1
        b["pnl"] = float(b["pnl"]) + t.pnl_points
    return buckets


def _write_csv(path: Path, trades: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "entry_at",
                "exit_at",
                "symbol",
                "direction",
                "entry_price",
                "exit_price",
                "pnl_points",
                "exit_reason",
                "entry_reason",
            ]
        )
        for t in trades:
            writer.writerow(
                [
                    t.entry_at,
                    t.exit_at,
                    t.symbol,
                    t.direction,
                    t.entry_price,
                    t.exit_price,
                    t.pnl_points,
                    t.exit_reason,
                    t.entry_reason,
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S3 BLR breakout backtest (30 SL / 60 TP, optional day-done filter)."
    )
    parser.add_argument("--years", type=float, default=3.0, help="Lookback years if --from/--to omitted")
    parser.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD")
    parser.add_argument("--indices", default="NIFTY", help="Comma list (default NIFTY)")
    parser.add_argument("--user", default="AK07")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--stop-after-first-tp",
        action="store_true",
        help="Match live Pine: no more entries after 1st trade of day hits target",
    )
    parser.add_argument("--json-out", metavar="PATH", help="Write full JSON report")
    parser.add_argument("--csv-out", metavar="PATH", help="Write trade list CSV")
    parser.add_argument("--verbose-trades", action="store_true")
    args = parser.parse_args()

    if args.stop_after_first_tp:
        os.environ["BREAKOUT_STOP_AFTER_FIRST_TP"] = "1"
    else:
        os.environ["BREAKOUT_STOP_AFTER_FIRST_TP"] = "0"

    end = _parse_date(args.to_date) if args.to_date else datetime.now(IST).date()
    if args.from_date:
        start = _parse_date(args.from_date)
    else:
        start = end - timedelta(days=int(args.years * 365.25))

    indices = {x.strip().upper() for x in args.indices.split(",") if x.strip()}
    day_done = args.stop_after_first_tp

    print("=" * 72)
    print("S3 BLR Breakout Backtest (Pine v8 aligned)")
    print(f"Period     : {start} -> {end}")
    print(f"Indices    : {', '.join(sorted(indices))}")
    print(f"Sizing     : fixed SL 30 / TP 60 (1:2)")
    print(f"Band       : NIFTY 0.211% auto (server default)")
    print(f"Max trades : 3/day · no entry after {os.environ.get('BREAKOUT_NO_ENTRY_AFTER_IST', '13:00')} · flat 14:55")
    print(f"Day done   : {'ON (1st TP stops day)' if day_done else 'OFF (no filter — default)'}")
    print("=" * 72)
    print()

    try:
        report = run_backtest(
            start=start,
            end=end,
            strategies={"s3"},
            indices=indices,
            username=args.user,
            use_cache=not args.no_cache,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    trades = report.trades
    summary = report.summary()
    wins = summary["wins"]
    losses = summary["losses"]
    total = summary["total_trades"]
    wr = summary["win_rate_pct"]
    pts = summary["total_pts"]

    gw = sum(t.pnl_points for t in trades if t.pnl_points > 0.01)
    gl = -sum(t.pnl_points for t in trades if t.pnl_points < -0.01)
    pf = gw / gl if gl > 0 else float("inf")

    print(f"Trades: {total}  Wins: {wins}  Losses: {losses}  WR: {wr}%")
    print(f"Total PnL: {pts:+.2f} pts  Profit factor: {pf:.2f}")
    print()

    for sym in sorted(indices):
        sym_trades = [t for t in trades if t.symbol == sym]
        if not sym_trades:
            continue
        sw = sum(1 for t in sym_trades if t.pnl_points > 0.01)
        sp = sum(t.pnl_points for t in sym_trades)
        print(f"  {sym}: {len(sym_trades)} trades, WR {100*sw/len(sym_trades):.1f}%, {sp:+.1f} pts")

    print()
    print("Exit breakdown:")
    for key, row in sorted(_exit_breakdown(trades).items(), key=lambda x: -float(x[1]["pnl"])):
        print(f"  {key:<22} {int(row['count']):>5} trades  {float(row['pnl']):>+10.1f} pts")

    if args.verbose_trades and trades:
        print()
        print("Trades:")
        for t in trades:
            print(
                f"  {t.entry_at[:16]}  {t.symbol} {t.direction:5s}  "
                f"{t.pnl_points:+7.2f}  [{t.exit_reason}]"
            )

    if args.json_out:
        out = Path(args.json_out)
        payload = {
            "config": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "indices": sorted(indices),
                "sizing": "fixed_sl_tp",
                "sl_pts": 30,
                "tp_pts": 60,
                "stop_after_first_tp": day_done,
            },
            "summary": summary,
            "profit_factor": round(pf, 3),
            "exit_breakdown": _exit_breakdown(trades),
            "trades": [t.as_dict() for t in trades],
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {out}")

    if args.csv_out:
        csv_path = Path(args.csv_out)
        _write_csv(csv_path, trades)
        print(f"Wrote {csv_path}")

    return 0 if total or summary.get("notes") else 1


if __name__ == "__main__":
    raise SystemExit(main())
