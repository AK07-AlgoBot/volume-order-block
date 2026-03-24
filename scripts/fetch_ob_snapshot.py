#!/usr/bin/env python3
"""
Fetch OB% and OB volume directly from Upstox (same candles + logic as the bot).
Does not read orders.log or any other local trade logs.

Run from repo root:
  python scripts/fetch_ob_snapshot.py
  python scripts/fetch_ob_snapshot.py --json
  python scripts/fetch_ob_snapshot.py --scripts CRUDE NIFTY
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Repo root (parent of scripts/)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="OB% snapshot from Upstox API (no orders.log)")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    parser.add_argument(
        "--scripts",
        nargs="*",
        help="Subset of script names (default: all TRADING_CONFIG scripts)",
    )
    args = parser.parse_args()

    # Import after path fix; reduce noise when not --json
    from trading_bot import API_CONFIG, TRADING_CONFIG, TradingBot, UpstoxClient

    if args.json:
        logging.getLogger().handlers.clear()
        logging.basicConfig(level=logging.WARNING)

    client = UpstoxClient(API_CONFIG["access_token"], API_CONFIG["base_url"])
    bot = TradingBot(TRADING_CONFIG, client)

    all_scripts = list(TRADING_CONFIG.get("scripts", {}).items())
    if args.scripts:
        wanted = {s.upper() for s in args.scripts}
        all_scripts = [(n, k) for n, k in all_scripts if n.upper() in wanted]
        missing = wanted - {n.upper() for n, _ in all_scripts}
        if missing:
            print(f"Unknown script(s): {', '.join(sorted(missing))}", file=sys.stderr)
            sys.exit(2)

    rows = []
    for script_name, instrument_key in all_scripts:
        data = bot.process_script(script_name, instrument_key)
        df = data.get("df") if data else None
        if df is None or df.empty:
            rows.append({"script": script_name, "error": "no_data"})
            continue

        closed = bot._get_last_closed_candle_row(df)
        if closed is None:
            rows.append({"script": script_name, "error": "no_closed_candle"})
            continue

        ts = closed["timestamp"]
        sig = int(closed.get("signal", 0) or 0)
        ema_side = "BUY" if sig == 1 else ("SELL" if sig == -1 else "NONE")

        buy_pct, buy_vol = bot._compute_chart_ob_snapshot(df, ts, "BUY")
        sell_pct, sell_vol = bot._compute_chart_ob_snapshot(df, ts, "SELL")

        rows.append(
            {
                "script": script_name,
                "instrument_key": data.get("instrument_key", instrument_key),
                "candle_ts": str(ts),
                "close": float(closed["close"]),
                "ema_side": ema_side,
                "buy_ob_pct": buy_pct,
                "buy_ob_vol": buy_vol,
                "sell_ob_pct": sell_pct,
                "sell_ob_vol": sell_vol,
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return

    print("OB snapshot from Upstox (last closed signal candle) — not from orders.log\n")
    for r in rows:
        if "error" in r:
            print(f"{r['script']}: ERROR {r['error']}")
            continue
        print(
            f"{r['script']:12}  candle={r['candle_ts']}  close={r['close']:.2f}  "
            f"ema_side={r['ema_side']}"
        )
        print(
            f"             BUY  OB%={r['buy_ob_pct']}  vol={r['buy_ob_vol']}  |  "
            f"SELL OB%={r['sell_ob_pct']}  vol={r['sell_ob_vol']}"
        )
        print()


if __name__ == "__main__":
    main()
