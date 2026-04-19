"""
Kite historical backtest for swing/trap strategy (30m swings, 5m entry, optional 1m confirm).

Usage:
  python src/scripts/backtest_swing_trap_kite.py --user AK07 --script NIFTY --days 5 \\
    --csv-out tmp/swing_trap_backtest.csv --json-out tmp/swing_trap_backtest.json
"""

from __future__ import annotations

import argparse
import sys
import time as pytime
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "src", REPO_ROOT / "src" / "lib"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from kite_fut_instrument import resolve_kite_instrument_token
from kite_rest_candles import fetch_historical_raw, kite_candles_to_dataframe, map_bot_interval_to_kite
from strategy.swing_trap.backtest_runner import run_swing_trap_backtest
from strategy.swing_trap.config import SwingTrapConfig
from strategy.swing_trap.reporter import trades_to_csv, trades_to_json
from zerodha_credentials_store import load_zerodha_credentials_for_user

IST = ZoneInfo("Asia/Kolkata")


def _fetch_chunked(
    api_key: str,
    access_token: str,
    token: int,
    kite_interval: str,
    from_dt: datetime,
    to_dt: datetime,
    *,
    prefer_cont: str = "1",
    chunk_days: int = 3,
    max_retries: int = 4,
) -> list:
    out: list = []
    cur = from_dt
    while cur < to_dt:
        end = min(to_dt, cur + timedelta(days=max(1, chunk_days)))
        rows = None
        last_err = None
        for attempt in range(max_retries):
            for cont in (prefer_cont, "0") if prefer_cont != "0" else ("0",):
                try:
                    rows = fetch_historical_raw(
                        api_key, access_token, token, kite_interval, cur, end, continuous=cont, oi="0"
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    rows = None
            if rows is not None:
                break
            pytime.sleep(1.5 * (2**attempt))
        if rows is None and last_err is not None:
            raise last_err
        out.extend(rows or [])
        pytime.sleep(0.2)
        cur = end
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Swing/trap strategy backtest (Kite)")
    ap.add_argument("--user", default="AK07")
    ap.add_argument("--script", default="NIFTY", help="NIFTY, BANKNIFTY, SENSEX, ...")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--csv-out", type=Path, default=REPO_ROOT / "tmp" / "swing_trap_backtest.csv")
    ap.add_argument("--json-out", type=Path, default=REPO_ROOT / "tmp" / "swing_trap_backtest.json")
    args = ap.parse_args()

    creds = load_zerodha_credentials_for_user(args.user)
    api_key = (creds.get("api_key") or "").strip()
    access_token = (creds.get("access_token") or "").strip()
    if not api_key or not access_token:
        raise SystemExit(f"Missing Kite credentials for user={args.user}")

    tok = resolve_kite_instrument_token(args.script, api_key, access_token)
    if not tok:
        raise SystemExit(f"No futures token for script={args.script}")

    now = datetime.now(IST)
    from_dt = now - timedelta(days=max(2, int(args.days)))
    to_dt = now

    i1 = map_bot_interval_to_kite("1minute")
    i5 = map_bot_interval_to_kite("5minute")
    i30 = map_bot_interval_to_kite("30minute")

    raw1 = _fetch_chunked(api_key, access_token, tok, i1, from_dt, to_dt, prefer_cont="1")
    raw5 = _fetch_chunked(api_key, access_token, tok, i5, from_dt, to_dt, prefer_cont="1")
    raw30 = _fetch_chunked(api_key, access_token, tok, i30, from_dt, to_dt, prefer_cont="1")

    df1 = kite_candles_to_dataframe(raw1) or pd.DataFrame()
    df5 = kite_candles_to_dataframe(raw5) or pd.DataFrame()
    df30 = kite_candles_to_dataframe(raw30) or pd.DataFrame()

    cfg = SwingTrapConfig()
    trades = run_swing_trap_backtest(df1, df5, df30, cfg)

    trades_to_csv(args.csv_out, trades)
    trades_to_json(args.json_out, trades)

    total_pts = sum(t.total_points for t in trades)
    print(
        f"Swing/trap backtest | user={args.user} | script={args.script} | days={args.days} | trades={len(trades)} | "
        f"total_points={total_pts:.2f}"
    )
    print(f"CSV={args.csv_out}")
    print(f"JSON={args.json_out}")


if __name__ == "__main__":
    main()
