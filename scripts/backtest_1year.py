#!/usr/bin/env python3
"""
1-Year Full Strategy Backtest — AK07 Trading System
=====================================================
Runs all 5 strategies (S1 S2 S3 S7 S8) on 1 year of historical
Upstox spot candles and prints a per-strategy × per-index breakdown.

WARNING: NOT FINANCIAL ADVICE. For research purposes only.

Run
---
  cd volume-order-block
  $env:PYTHONPATH="src\\server\\src"
  $env:PYTHONIOENCODING="utf-8"
  python scripts/backtest_1year.py
  python scripts/backtest_1year.py --from 2025-06-23 --to 2026-06-23
  python scripts/backtest_1year.py --json-out results_1y.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

os.environ["AK07_MOCK"] = "0"
os.environ["AK07_MOCK_MODE"] = "0"
os.environ["AK07_PAPER_TRADING"] = "1"

from app.services.backtest_runner import STRATEGY_RUNNERS, run_backtest  # noqa: E402
from app.services.upstox_engine import INDEX_CONFIGS  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

INDENT = "  "
DIV = "=" * 70
SUB = "-" * 70

LOT_SIZES = {
    "NIFTY": INDEX_CONFIGS["NIFTY"].lot_size,
    "BANKNIFTY": INDEX_CONFIGS["BANKNIFTY"].lot_size,
    "SENSEX": INDEX_CONFIGS["SENSEX"].lot_size,
}
def _inr(pts: float, symbol: str) -> float:
    """Convert index points to INR (1 lot). pts × lot_size = direct options P&L."""
    lot = LOT_SIZES.get(symbol, 65)
    return pts * lot


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "—"


def print_table(header: str, rows: list[dict]) -> None:
    """Print a simple ASCII table."""
    print(f"\n{header}")
    print(SUB)
    if not rows:
        print("  (no trades)")
        return
    # Determine column widths
    col_keys = list(rows[0].keys())
    widths = {k: max(len(k), max(len(str(r[k])) for r in rows)) for k in col_keys}
    fmt = INDENT + "  ".join(f"{{:<{widths[k]}}}" for k in col_keys)
    print(fmt.format(*col_keys))
    print(INDENT + "  ".join("-" * widths[k] for k in col_keys))
    for r in rows:
        print(fmt.format(*[str(r[k]) for k in col_keys]))


def analyze(trades: list, label: str) -> dict:
    """Return summary stats for a list of trade dicts."""
    if not trades:
        return {"label": label, "trades": 0, "wins": 0, "losses": 0, "wr": "—",
                "total_pts": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "expectancy": 0.0}
    pts = [t["pnl_points"] for t in trades]
    wins = [p for p in pts if p > 0.01]
    losses = [p for p in pts if p < -0.01]
    total_pts = round(sum(pts), 2)
    avg_win = round(statistics.mean(wins), 2) if wins else 0.0
    avg_loss = round(statistics.mean(losses), 2) if losses else 0.0
    wr_float = len(wins) / len(pts) if pts else 0
    expectancy = round(wr_float * avg_win + (1 - wr_float) * avg_loss, 2)
    return {
        "label": label,
        "trades": len(pts),
        "wins": len(wins),
        "losses": len(losses),
        "wr": _pct(len(wins), len(pts)),
        "total_pts": total_pts,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="1-Year AK07 strategy backtest")
    parser.add_argument("--from", dest="from_date", default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--strategies", default="all",
                        help="all | s1,s2,s3,s7,s8  (default all)")
    parser.add_argument("--indices", default="NIFTY,BANKNIFTY,SENSEX")
    parser.add_argument("--json-out", metavar="PATH", default=None)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    end = date.fromisoformat(args.to_date) if args.to_date else datetime.now(IST).date()
    start = date.fromisoformat(args.from_date) if args.from_date else end - timedelta(days=365)
    indices = {x.strip().upper() for x in args.indices.split(",") if x.strip()}
    if args.strategies.strip().lower() == "all":
        strategies = set(STRATEGY_RUNNERS.keys())
    else:
        strategies = {s.strip().lower() for s in args.strategies.split(",") if s.strip()}

    print(f"\n{DIV}")
    print(f"  AK07 1-Year Backtest  {start} -> {end}")
    print(f"  Strategies : {', '.join(sorted(strategies)).upper()}")
    print(f"  Indices    : {', '.join(sorted(indices))}")
    print(f"{DIV}")
    print("  Fetching historical candles from Upstox (may take 1-2 min)...")

    report = run_backtest(
        start=start,
        end=end,
        strategies=strategies,
        indices=indices,
        use_cache=not args.no_cache,
    )

    for note in report.notes:
        print(f"  NOTE: {note}")

    # Organise trades: strategy -> symbol -> [trade]
    by_strat_sym: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t in report.trades:
        by_strat_sym[t.strategy][t.symbol].append(t.__dict__ if hasattr(t, "__dict__") else {
            "strategy": t.strategy, "symbol": t.symbol,
            "direction": t.direction, "pnl_points": t.pnl_points,
            "entry_at": t.entry_at, "exit_at": t.exit_at, "exit_reason": t.exit_reason,
        })

    strategy_display = {
        "Strategy 1 — AK07 OI": "S1 — AK07 OI",
        "Strategy 2 — SMC+CRT": "S2 — SMC+CRT",
        "Strategy 3 — BLR Breakout": "S3 — BLR Breakout",
        "Strategy 7 — ORB+": "S7 — ORB+ ADX",
        "Strategy 8 — CHOCH": "S8 — CHOCH",
    }

    all_index_totals: dict[str, float] = defaultdict(float)
    all_index_inr: dict[str, float] = defaultdict(float)
    grand_rows = []

    for strat_key, sym_trades in sorted(by_strat_sym.items()):
        label = strategy_display.get(strat_key, strat_key)
        print(f"\n\n{'#' * 70}")
        print(f"  {label}")
        print(f"{'#' * 70}")

        strat_rows = []
        strat_pts = 0.0
        strat_inr = 0.0

        for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
            trades = sym_trades.get(sym, [])
            s = analyze(trades, sym)
            if not trades:
                strat_rows.append({
                    "Index": sym,
                    "Trades": 0, "Wins": 0, "Losses": 0, "WR%": "—",
                    "Total pts": "+0.0", "Avg win": "+0.0", "Avg loss": "+0.0",
                    "Expect/t": "+0.00", "INR/day": "+0",
                })
                continue

            trading_days = len({t.get("entry_at", t.entry_at if hasattr(t, "entry_at") else "")[:10]
                                 for t in trades if isinstance(t, dict)
                                 or True}) if trades else 1
            # Count unique trading days more accurately
            entries = []
            for t in trades:
                at = t.entry_at if hasattr(t, "entry_at") else t.get("entry_at", "")
                entries.append(str(at)[:10])
            trading_days_uniq = max(1, len(set(entries)))

            inr_total = _inr(s["total_pts"], sym)
            inr_per_day = inr_total / trading_days_uniq
            strat_pts += s["total_pts"]
            strat_inr += inr_total
            all_index_totals[sym] += s["total_pts"]
            all_index_inr[sym] += inr_total

            strat_rows.append({
                "Index": sym,
                "Trades": s["trades"],
                "Wins": s["wins"],
                "Losses": s["losses"],
                "WR%": s["wr"],
                "Total pts": f"{s['total_pts']:+.1f}",
                "Avg win": f"{s['avg_win']:+.1f}",
                "Avg loss": f"{s['avg_loss']:+.1f}",
                "Expect/t": f"{s['expectancy']:+.2f}",
                "INR/day": f"+{inr_per_day:,.0f}",
            })

        print_table(f"  Per-Index Breakdown — {label}", strat_rows)

        # Direction split
        all_strat_trades = [t for sym_t in sym_trades.values() for t in sym_t]
        long_trades = [t for t in all_strat_trades
                       if (t.direction if hasattr(t, "direction") else t.get("direction")) == "LONG"]
        short_trades = [t for t in all_strat_trades
                        if (t.direction if hasattr(t, "direction") else t.get("direction")) == "SHORT"]
        sl = analyze(long_trades, "LONG")
        ss = analyze(short_trades, "SHORT")
        dir_rows = [
            {"Direction": "LONG",  "Trades": sl["trades"], "WR%": sl["wr"], "Total pts": f"{sl['total_pts']:+.1f}", "Expect/t": f"{sl['expectancy']:+.2f}"},
            {"Direction": "SHORT", "Trades": ss["trades"], "WR%": ss["wr"], "Total pts": f"{ss['total_pts']:+.1f}", "Expect/t": f"{ss['expectancy']:+.2f}"},
        ]
        print_table("  Direction Breakdown", dir_rows)

        grand_rows.append({
            "Strategy": label,
            "Trades": len(all_strat_trades),
            "WR%": _pct(len([t for t in all_strat_trades
                              if (t.pnl_points if hasattr(t, "pnl_points") else t.get("pnl_points", 0)) > 0.01]),
                        len(all_strat_trades)),
            "Total pts": f"{strat_pts:+.1f}",
            "INR(1lot)": f"+{strat_inr:,.0f}",
        })

    # ── Grand summary ──────────────────────────────────────────────────────────
    print(f"\n\n{DIV}")
    print("  GRAND SUMMARY — ALL STRATEGIES × ALL INDICES")
    print(DIV)
    print_table("  Strategy Totals", grand_rows)

    idx_rows = []
    for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        pts = all_index_totals[sym]
        inr = all_index_inr[sym]
        trading_days_approx = 250  # ~252 trading days/year
        idx_rows.append({
            "Index": sym,
            "Total pts": f"{pts:+.1f}",
            "INR(1lot)": f"+{inr:,.0f}",
            "INR/day(1lot)": f"+{inr/trading_days_approx:,.0f}",
            "Lots for Rs.3k/day": f"~{max(1, round(3000 / max(1, inr / trading_days_approx)))}",
        })
    print_table("  Per-Index Totals (all strategies combined)", idx_rows)

    total_trades = len(report.trades)
    total_wins = sum(1 for t in report.trades
                     if (t.pnl_points if hasattr(t, "pnl_points") else t.get("pnl_points", 0)) > 0.01)
    total_pts = sum(t.pnl_points if hasattr(t, "pnl_points") else t.get("pnl_points", 0)
                    for t in report.trades)
    total_inr = sum(all_index_inr.values())

    print(f"\n  {'='*50}")
    print(f"  OVERALL  {total_trades} trades | WR {_pct(total_wins, total_trades)} | "
          f"{total_pts:+.1f} pts | INR(1lot) +{total_inr:,.0f}")
    print(f"  {'='*50}")
    print(f"\n  WARNING: NOT financial advice. For research only.")
    print()

    # ── JSON export ────────────────────────────────────────────────────────────
    if args.json_out:
        payload = {
            "start": start.isoformat(), "end": end.isoformat(),
            "total_trades": total_trades,
            "total_pts": round(total_pts, 2),
            "grand_rows": grand_rows,
            "idx_rows": idx_rows,
            "trades": [
                {
                    "strategy": t.strategy, "symbol": t.symbol,
                    "direction": t.direction, "pnl_points": t.pnl_points,
                    "entry_at": t.entry_at, "exit_at": t.exit_at,
                    "exit_reason": t.exit_reason, "entry_reason": t.entry_reason,
                }
                for t in report.trades
            ],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"  Results written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
