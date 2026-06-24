#!/usr/bin/env python3
"""Compare S3 BLR backtest variants (baseline vs offset tweaks).

Runs separate subprocesses so BREAKOUT_* env vars reload cleanly.

Examples:
  python scripts/backtest_s3_compare.py
  python scripts/backtest_s3_compare.py --from 2025-06-23 --to 2026-06-23
  python scripts/backtest_s3_compare.py --days 90
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

VARIANTS: list[tuple[str, str, dict[str, str]]] = [
    ("baseline", "baseline (0/0)", {"BREAKOUT_GREEN_OFFSET": "0", "BREAKOUT_RED_OFFSET": "0"}),
    ("red_m3", "BLR red -3", {"BREAKOUT_GREEN_OFFSET": "0", "BREAKOUT_RED_OFFSET": "-3"}),
    ("gr3_rm3", "BLR green +3 / red -3", {"BREAKOUT_GREEN_OFFSET": "3", "BREAKOUT_RED_OFFSET": "-3"}),
]


def _run_variant(
    slug: str,
    label: str,
    env_overrides: dict[str, str],
    *,
    from_date: str | None,
    to_date: str | None,
    days: int,
) -> dict:
    out_path = REPO / f"backtest_s3_{slug}.json"
    cmd = [
        sys.executable,
        str(REPO / "scripts" / "backtest_ak07.py"),
        "--strategies",
        "s3",
        "--json-out",
        str(out_path),
    ]
    if from_date and to_date:
        cmd.extend(["--from", from_date, "--to", to_date])
    else:
        cmd.extend(["--days", str(days)])

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "src" / "server" / "src")
    env["AK07_MOCK"] = "0"
    env["AK07_MOCK_MODE"] = "0"
    env["AK07_PAPER_TRADING"] = "1"
    env.update(env_overrides)

    print(f"\n>>> {label}  (GREEN={env_overrides['BREAKOUT_GREEN_OFFSET']}, RED={env_overrides['BREAKOUT_RED_OFFSET']})")
    proc = subprocess.run(cmd, env=env, cwd=str(REPO), capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr or "backtest failed", file=sys.stderr)
        return {"label": label, "error": proc.stderr or "failed"}

    if not out_path.exists():
        return {"label": label, "error": "no json output"}

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    summary = payload.get("summary") or {}
    by_idx: dict[str, dict] = {}
    for trade in payload.get("trades") or []:
        sym = trade["symbol"]
        bucket = by_idx.setdefault(sym, {"trades": 0, "wins": 0, "pts": 0.0})
        bucket["trades"] += 1
        bucket["pts"] += float(trade["pnl_points"])
        if float(trade["pnl_points"]) > 0.01:
            bucket["wins"] += 1

    return {
        "label": label,
        "green_offset": env_overrides["BREAKOUT_GREEN_OFFSET"],
        "red_offset": env_overrides["BREAKOUT_RED_OFFSET"],
        "total_trades": summary.get("total_trades", 0),
        "win_rate_pct": summary.get("win_rate_pct", 0),
        "total_pts": summary.get("total_pts", 0),
        "by_index": by_idx,
        "json_out": str(out_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare S3 BLR offset variants.")
    parser.add_argument("--days", type=int, default=365, help="Lookback days if --from/--to omitted")
    parser.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD")
    args = parser.parse_args()

    print("S3 BLR offset comparison (live-aligned backtest — no ADX/prev-day filters)")
    if args.from_date and args.to_date:
        print(f"Range: {args.from_date} -> {args.to_date}")
    else:
        print(f"Range: last {args.days} calendar days")

    rows = []
    for slug, label, overrides in VARIANTS:
        rows.append(
            _run_variant(
                slug,
                label,
                overrides,
                from_date=args.from_date,
                to_date=args.to_date,
                days=args.days,
            )
        )

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    hdr = f"{'Variant':<28} {'Trades':>7} {'WR%':>7} {'Total pts':>12} {'NIFTY':>10} {'BNF':>10} {'SENSEX':>10}"
    print(hdr)
    print("-" * len(hdr))
    for row in rows:
        if row.get("error"):
            print(f"{row['label']:<28} ERROR: {row['error'][:40]}")
            continue
        by = row.get("by_index") or {}
        print(
            f"{row['label']:<28} {row['total_trades']:>7} {row['win_rate_pct']:>6.1f}% "
            f"{row['total_pts']:>+12.1f} "
            f"{by.get('NIFTY', {}).get('pts', 0):>+10.1f} "
            f"{by.get('BANKNIFTY', {}).get('pts', 0):>+10.1f} "
            f"{by.get('SENSEX', {}).get('pts', 0):>+10.1f}"
        )

    compare_path = REPO / "backtest_s3_compare.json"
    compare_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nFull comparison written to {compare_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
