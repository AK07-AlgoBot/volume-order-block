#!/usr/bin/env python3
"""
Compare S8 CHOCH structure logic: strict (old) vs relaxed (new) over 3 years.

Runs the shared backtest_runner path twice with CHOCH_RELAXED_STRUCTURE=0/1,
compares against the saved baseline in backtest_3year.json, and warns if the
new relaxed logic materially drops performance.

Run:
  cd volume-order-block
  $env:PYTHONPATH="src\\server\\src"
  python scripts/backtest_choch_compare.py
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

from app.services.backtest_runner import S8_LABEL, run_backtest  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
BASELINE_PATH = REPO / "backtest_3year.json"
S8_STRATEGY_KEY = "Strategy 8 — CHOCH"
DROP_PTS_THRESHOLD = 250.0  # warn if relaxed loses >250 pts vs strict/baseline
DROP_WR_THRESHOLD = 2.0  # warn if win rate drops >2 pp vs baseline


def _s8_stats(report) -> dict:
    trades = [t for t in report.trades if t.strategy == S8_LABEL]
    if not trades:
        return {"trades": 0, "wins": 0, "losses": 0, "wr_pct": 0.0, "total_pts": 0.0}
    wins = sum(1 for t in trades if t.pnl_points > 0.01)
    losses = sum(1 for t in trades if t.pnl_points < -0.01)
    total_pts = round(sum(t.pnl_points for t in trades), 1)
    wr = round(100.0 * wins / len(trades), 1)
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "wr_pct": wr,
        "total_pts": total_pts,
    }


def _load_baseline() -> dict | None:
    if not BASELINE_PATH.is_file():
        return None
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    for row in data.get("grand_rows") or []:
        if "S8" in str(row.get("Strategy", "")):
            pts_raw = str(row.get("Total pts", "0")).replace("+", "").replace(",", "")
            return {
                "start": data.get("start"),
                "end": data.get("end"),
                "trades": int(row.get("Trades", 0)),
                "wr_pct": float(str(row.get("WR%", "0")).replace("%", "")),
                "total_pts": float(pts_raw),
            }
    return None


def _run(label: str, relaxed: bool, start: date, end: date, use_cache: bool):
    os.environ["CHOCH_RELAXED_STRUCTURE"] = "1" if relaxed else "0"
    print(f"\n  Running {label} (CHOCH_RELAXED_STRUCTURE={os.environ['CHOCH_RELAXED_STRUCTURE']})...")
    report = run_backtest(
        start=start,
        end=end,
        strategies={"s8"},
        indices={"NIFTY", "BANKNIFTY", "SENSEX"},
        use_cache=use_cache,
    )
    stats = _s8_stats(report)
    print(
        f"    {stats['trades']} trades | WR {stats['wr_pct']:.1f}% | "
        f"total {stats['total_pts']:+.1f} pts"
    )
    return stats


def _print_row(name: str, stats: dict) -> None:
    print(
        f"  {name:<22} {stats['trades']:>5} trades  "
        f"WR {stats['wr_pct']:>5.1f}%  total {stats['total_pts']:>+8.1f} pts"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="S8 CHOCH strict vs relaxed 3-year compare")
    parser.add_argument("--from", dest="from_date", default="2023-06-23", metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--json-out", metavar="PATH", default=None)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    end = date.fromisoformat(args.to_date) if args.to_date else datetime.now(IST).date()
    start = date.fromisoformat(args.from_date)

    print("=" * 70)
    print(f"  S8 CHOCH structure compare  {start} -> {end}")
    print("=" * 70)

    baseline = _load_baseline()
    strict = _run("STRICT (old logic)", relaxed=False, start=start, end=end, use_cache=not args.no_cache)
    relaxed = _run("RELAXED (new logic)", relaxed=True, start=start, end=end, use_cache=not args.no_cache)

    print("\n" + "-" * 70)
    print("  COMPARISON")
    print("-" * 70)
    if baseline:
        _print_row("Baseline (saved)", baseline)
    _print_row("Strict (re-run)", strict)
    _print_row("Relaxed (new)", relaxed)

    delta_vs_strict = relaxed["total_pts"] - strict["total_pts"]
    delta_wr_vs_strict = relaxed["wr_pct"] - strict["wr_pct"]
    print(f"\n  Relaxed vs strict : {delta_vs_strict:+.1f} pts | WR {delta_wr_vs_strict:+.1f} pp")

    warnings: list[str] = []
    if delta_vs_strict < -DROP_PTS_THRESHOLD:
        warnings.append(
            f"Relaxed logic is {abs(delta_vs_strict):.0f} pts WORSE than strict "
            f"(threshold {DROP_PTS_THRESHOLD:.0f} pts)."
        )
    if baseline:
        delta_vs_baseline = relaxed["total_pts"] - baseline["total_pts"]
        delta_wr_baseline = relaxed["wr_pct"] - baseline["wr_pct"]
        print(
            f"  Relaxed vs baseline: {delta_vs_baseline:+.1f} pts | WR {delta_wr_baseline:+.1f} pp"
        )
        if delta_vs_baseline < -DROP_PTS_THRESHOLD:
            warnings.append(
                f"Relaxed logic is {abs(delta_vs_baseline):.0f} pts below saved 3y baseline "
                f"({baseline['total_pts']:+.1f} pts)."
            )
        if delta_wr_baseline < -DROP_WR_THRESHOLD:
            warnings.append(
                f"Relaxed win rate dropped {abs(delta_wr_baseline):.1f} pp vs baseline "
                f"({baseline['wr_pct']:.1f}%)."
            )

    if delta_vs_strict > 0:
        print("\n  OK: Relaxed structure improves total points vs strict re-run.")
    elif not warnings:
        print("\n  OK: Relaxed is within tolerance of strict/baseline.")

    if warnings:
        print("\n  *** PERFORMANCE DROP WARNING ***")
        for w in warnings:
            print(f"  - {w}")
        rc = 1
    else:
        rc = 0

    if args.json_out:
        out = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "baseline": baseline,
            "strict": strict,
            "relaxed": relaxed,
            "delta_pts_strict": round(delta_vs_strict, 1),
            "delta_wr_strict": round(delta_wr_vs_strict, 1),
            "warnings": warnings,
        }
        Path(args.json_out).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\n  Wrote {args.json_out}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
