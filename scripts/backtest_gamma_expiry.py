#!/usr/bin/env python3
"""3-year gamma expiry backtest — grid search + refined config export.

Spot-only hero proxy on expiry days (no historical option chain).
Writes src/server/data/gamma_expiry_config.json for live observer.

  python scripts/backtest_gamma_expiry.py
  python scripts/backtest_gamma_expiry.py --from 2023-06-23 --to 2026-06-23
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

os.environ["AK07_MOCK"] = "0"
os.environ["AK07_PAPER_TRADING"] = "1"

from app.config.paths import server_root  # noqa: E402
from app.services.backtest_data import HistoricalDataClient, parse_candle_ts  # noqa: E402
from app.services.expiry_calendar import iter_expiry_days  # noqa: E402
from app.services.gamma_expiry_analytics import (  # noqa: E402
    GammaConfig,
    grid_search_configs,
    refine_gamma_config,
    simulate_hero_on_expiry_day,
)
from app.services.upstox_engine import INDEX_CONFIGS  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _group_candles_by_day(candles: list[dict[str, float]]) -> dict[date, list[dict[str, float]]]:
    by_day: dict[date, list[dict[str, float]]] = defaultdict(list)
    for c in candles:
        ts = parse_candle_ts(c["timestamp"])
        by_day[ts.date()].append(c)
    for day in by_day:
        by_day[day].sort(key=lambda x: x["timestamp"])
    return by_day


def main() -> int:
    parser = argparse.ArgumentParser(description="Gamma expiry hero backtest + param refine")
    parser.add_argument("--from", dest="from_date", default="2023-06-23")
    parser.add_argument("--to", dest="to_date", default=None)
    parser.add_argument("--user", default="AK07")
    parser.add_argument("--json-out", default=str(REPO / "backtest_gamma_3y.json"))
    args = parser.parse_args()

    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date) if args.to_date else datetime.now(IST).date()

    print(f"Gamma expiry backtest  {start} -> {end}")
    print("Fetching 5m candles (cached)...")

    try:
        hclient = HistoricalDataClient(username=args.user)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    configs = grid_search_configs()
    trades_by_key: dict[tuple, list] = {(
        c.pin_distance_pct,
        c.min_idr_pct,
        c.otm_strikes,
        c.hero_tp_mult,
    ): [] for c in configs}

    all_trades: list[dict] = []

    for code, cfg in INDEX_CONFIGS.items():
        exp_days = iter_expiry_days(code, start, end)
        if not exp_days:
            continue
        print(f"  {code}: {len(exp_days)} expiry days")
        candles = hclient.fetch_5m(cfg.spot_instrument_key, start, end)
        by_day = _group_candles_by_day(candles)

        for exp_day in exp_days:
            day_candles = by_day.get(exp_day)
            if not day_candles:
                continue
            for params in configs:
                trade = simulate_hero_on_expiry_day(
                    cfg=cfg,
                    day=exp_day,
                    session_candles=day_candles,
                    params=params,
                )
                if trade:
                    key = (
                        params.pin_distance_pct,
                        params.min_idr_pct,
                        params.otm_strikes,
                        params.hero_tp_mult,
                    )
                    trades_by_key[key].append(trade)

    refined = refine_gamma_config(trades_by_key)
    refined_key = (
        refined.pin_distance_pct,
        refined.min_idr_pct,
        refined.otm_strikes,
        refined.hero_tp_mult,
    )
    refined_trades = trades_by_key.get(refined_key, [])

    for t in refined_trades:
        all_trades.append(
            {
                "index": t.index_code,
                "day": t.day.isoformat(),
                "direction": t.direction,
                "option_type": t.option_type,
                "strike": t.strike,
                "entry_premium": t.entry_premium,
                "exit_premium": t.exit_premium,
                "pnl_premium": t.pnl_premium,
                "entry_at": t.entry_at,
                "exit_at": t.exit_at,
                "exit_reason": t.exit_reason,
                "blast_score": t.blast_score,
            }
        )

    wins = sum(1 for t in refined_trades if t.pnl_premium > 0.01)
    losses = sum(1 for t in refined_trades if t.pnl_premium < -0.01)
    total_prem = sum(t.pnl_premium for t in refined_trades)
    n = len(refined_trades)

    summary = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "refined_config": {
            "pin_distance_pct": refined.pin_distance_pct,
            "min_idr_pct": refined.min_idr_pct,
            "otm_strikes": refined.otm_strikes,
            "hero_tp_mult": refined.hero_tp_mult,
            "min_blast_score": refined.min_blast_score,
            "iv_assumption": refined.iv_assumption,
        },
        "refined_trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / n, 1) if n else 0,
        "total_premium_pts": round(total_prem, 2),
        "expectancy_premium": round(total_prem / n, 2) if n else 0,
        "note": "Spot-only BS proxy — indicative, not exact option fills.",
    }

    print("\n" + "=" * 60)
    print("REFINED CONFIG (best grid cell with >=30 trades)")
    print("=" * 60)
    for k, v in summary["refined_config"].items():
        print(f"  {k}: {v}")
    print(f"\nTrades: {n}  WR: {summary['win_rate_pct']}%  "
          f"Total prem: {summary['total_premium_pts']:+.1f}  "
          f"Expect/trade: {summary['expectancy_premium']:+.2f}")

    config_path = server_root() / "data" / "gamma_expiry_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(summary["refined_config"], indent=2), encoding="utf-8")
    print(f"\nWrote live config -> {config_path}")

    payload = {"summary": summary, "trades": all_trades}
    Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Full report -> {args.json_out}")

    # Also publish summary to a well-known path for repo root
    (REPO / "gamma_expiry_config.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
