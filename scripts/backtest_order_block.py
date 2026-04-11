#!/usr/bin/env python3
"""
Walk-forward order-block backtest (last N calendar days, session-close snapshots).

Requires Zerodha credentials: env KITE_API_KEY + KITE_ACCESS_TOKEN, or
src/server/data/users/<user>/zerodha_credentials.json.

From repo root (PowerShell):
  $env:PYTHONPATH = "src\\server\\src;src\\lib"
  python scripts/backtest_order_block.py --days 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src" / "lib"))
sys.path.insert(0, str(REPO / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402
from app.services.kite_historical import fetch_historical, resolve_instrument  # noqa: E402
from order_block_backtest import (  # noqa: E402
    DirectionStats,
    merge_stats,
    session_end_ist,
    snapshot_analyze,
    trading_days_between,
)
from order_block_logic import rows_to_candles  # noqa: E402
from trading_script_constants import ORDER_BLOCK_BACKTEST_SYMBOLS  # noqa: E402


def _load_creds(user_safe: str) -> tuple[str, str, str]:
    ensure_repo_and_lib_on_path()
    key = (os.environ.get("KITE_API_KEY") or "").strip()
    tok = (os.environ.get("KITE_ACCESS_TOKEN") or "").strip()
    base = (os.environ.get("KITE_BASE_URL") or "").strip() or "https://api.kite.trade"
    if key and tok:
        return key, tok, base
    path = REPO / "src" / "server" / "data" / "users" / user_safe / "zerodha_credentials.json"
    if not path.is_file():
        raise SystemExit(
            f"Missing credentials: set KITE_API_KEY and KITE_ACCESS_TOKEN, or create {path}",
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    key = str(data.get("api_key") or "").strip()
    tok = str(data.get("access_token") or "").strip()
    base = str(data.get("base_url") or "").strip() or "https://api.kite.trade"
    if not key or not tok:
        raise SystemExit("zerodha_credentials.json must include api_key and access_token")
    return key, tok, base


def _fetch_all(
    api_key: str,
    access_token: str,
    base: str,
    token_id: int,
    now: datetime,
) -> dict[str, list]:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    out: dict[str, list] = {}
    windows = {
        "day": (now - timedelta(days=320), now),
        "60minute": (now - timedelta(days=70), now),
        "30minute": (now - timedelta(days=70), now),
        "15minute": (now - timedelta(days=70), now),
        "5minute": (now - timedelta(days=70), now),
    }
    for iv, (a, b) in windows.items():
        raw = fetch_historical(api_key, access_token, token_id, iv, a, b, base)
        out[iv] = raw
    return out


def backtest_one_symbol(
    symbol: str,
    api_key: str,
    access_token: str,
    base: str,
    days: int,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, DirectionStats, DirectionStats]:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    resolved = resolve_instrument(api_key, access_token, symbol)
    if not resolved:
        return (
            {"symbol": symbol, "error": "could not resolve instrument"},
            DirectionStats(),
            DirectionStats(),
        )

    _, token_id = resolved
    raw = _fetch_all(api_key, access_token, base, token_id, now)

    daily = rows_to_candles(raw["day"])
    h60 = rows_to_candles(raw["60minute"])
    m30 = rows_to_candles(raw["30minute"])
    m15 = rows_to_candles(raw["15minute"])
    m5 = rows_to_candles(raw["5minute"])

    end_calendar = now.date()
    start_calendar = end_calendar - timedelta(days=days)

    tdays = trading_days_between(daily, start_calendar, end_calendar)
    intra = DirectionStats()
    pos = DirectionStats()

    for d in tdays:
        as_of = session_end_ist(d)
        snap = snapshot_analyze(daily, h60, m30, m15, m5, as_of)
        if not snap:
            continue
        i = snap.get("intraday") or {}
        p = snap.get("positional") or {}
        intra.record(str(i.get("direction") or "none"), i.get("entry_probability"))
        pos.record(str(p.get("direction") or "none"), p.get("entry_probability"))

    info = {
        "symbol": symbol,
        "trading_days_in_window": len(tdays),
        "intraday": intra.to_dict(),
        "positional": pos.to_dict(),
    }
    return info, intra, pos


def main() -> None:
    p = argparse.ArgumentParser(description="Order-block walk-forward stats (last N days)")
    p.add_argument("--days", type=int, default=30, help="Calendar-day lookback (default 30)")
    p.add_argument("--user", type=str, default="AK07", help="User folder for zerodha_credentials.json")
    p.add_argument(
        "--symbols",
        type=str,
        default="",
        help="Comma-separated symbols (default: ORDER_BLOCK_BACKTEST_SYMBOLS)",
    )
    args = p.parse_args()

    api_key, access_token, base = _load_creds(args.user)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        symbols = list(ORDER_BLOCK_BACKTEST_SYMBOLS)

    print(f"Backtest: session-close snapshots over ~{args.days} calendar days")
    print(f"Symbols ({len(symbols)}): {', '.join(symbols)}")
    print(
        "Note: 'probability' is the dashboard score (rule-based), not empirical win rate.\n",
    )

    agg_intra = DirectionStats()
    agg_pos = DirectionStats()
    rows_out: list[dict[str, Any]] = []

    for sym in symbols:
        info, intra_st, pos_st = backtest_one_symbol(sym, api_key, access_token, base, args.days)
        rows_out.append(info)
        if info.get("error"):
            print(f"{sym}: ERROR — {info['error']}")
            continue
        print(f"=== {sym} - {info['trading_days_in_window']} sessions ===")
        print("  Intraday:   ", info["intraday"])
        print("  Positional:", info["positional"])
        print()
        agg_intra = merge_stats(agg_intra, intra_st)
        agg_pos = merge_stats(agg_pos, pos_st)

    print("--- All symbols combined ---")
    print("  Intraday:   ", agg_intra.to_dict())
    print("  Positional:", agg_pos.to_dict())

    out_path = REPO / "src" / "server" / "data" / "logs" / "order_block_backtest_last.json"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "days": args.days,
            "symbols": symbols,
            "per_symbol": rows_out,
            "combined": {
                "intraday": agg_intra.to_dict(),
                "positional": agg_pos.to_dict(),
            },
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print()
        print("Wrote", out_path)
    except OSError as e:
        print("Could not write JSON:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
