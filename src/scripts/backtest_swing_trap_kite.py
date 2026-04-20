"""
Kite historical backtest for swing/trap strategy (30m swings, 5m entry, optional 1m confirm).

Usage:
  python src/scripts/backtest_swing_trap_kite.py --user AK07 --scripts NIFTY,BANKNIFTY,SENSEX --days 5 \\
    --csv-out tmp/swing_trap_backtest.csv --json-out tmp/swing_trap_backtest.json

  When --scripts is omitted, all configured scripts are tested (35 currently).
  For each script, index token is preferred; fallback is futures token.
  Pass --futures to force futures-only resolution.
"""

from __future__ import annotations

import argparse
import sys
import time as pytime
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "src", REPO_ROOT / "src" / "lib"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
for p in (REPO_ROOT / "src" / "bot",):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from kite_fut_instrument import resolve_kite_instrument_token, resolve_nse_index_instrument_token
from kite_rest_candles import fetch_historical_raw, kite_candles_to_dataframe, map_bot_interval_to_kite
from strategy.swing_trap.backtest_runner import run_swing_trap_backtest
from strategy.swing_trap.config import SwingTrapConfig
from strategy.swing_trap.models import SwingTrapTrade
from strategy.swing_trap.reporter import trades_to_csv, trades_to_json
from trading_script_constants import AVAILABLE_SCRIPT_NAMES
from trading_bot import send_paper_trade_notification, telegram_notifications_enabled_for_user
from zerodha_credentials_store import load_zerodha_credentials_for_user

IST = ZoneInfo("Asia/Kolkata")


def _print_trade_levels_log(script: str, trades: list[SwingTrapTrade]) -> None:
    if not trades:
        print(f"[{script}] no swing-trap trades generated.")
        return

    print(f"[{script}] swing-trap trades={len(trades)}")
    for t in trades:
        meta = dict(t.meta or {})
        b_idx = meta.get("breakout_idx")
        r_idx = meta.get("retest_idx")
        tr_idx = meta.get("trap_idx")
        e_idx = meta.get("entry_idx")
        ref_mode = meta.get("reference_mode", "NA")
        ref_day = meta.get("reference_30m_day", "NA")
        print(
            f"[{script}] {t.side} entry={t.entry_ts.isoformat()} "
            f"30m_high={t.swing_high_ref:.2f} 30m_low={t.swing_low_ref:.2f} "
            f"breakout_lvl={t.breakout_level:.2f} sl={t.stop_loss:.2f} target={t.target_price:.2f} "
            f"pts={t.total_points:.2f} exit={t.exit_reason} "
            f"idx(b/r/t/e)=({b_idx}/{r_idx}/{tr_idx}/{e_idx}) "
            f"ref={ref_mode}:{ref_day}"
        )


def _notify_telegram_for_trades(user: str, script: str, trades: list[SwingTrapTrade], qty: float) -> None:
    if not trades:
        return
    if not telegram_notifications_enabled_for_user(user):
        return

    sent = 0
    for t in trades:
        meta = dict(t.meta or {})
        b_idx = meta.get("breakout_idx")
        r_idx = meta.get("retest_idx")
        tr_idx = meta.get("trap_idx")
        e_idx = meta.get("entry_idx")
        ref_mode = meta.get("reference_mode", "NA")
        ref_day = meta.get("reference_30m_day", "NA")
        exit_px = None
        if t.lot_exits:
            try:
                exit_px = float(t.lot_exits[-1].exit_price)
            except Exception:
                exit_px = None
        if exit_px is None:
            exit_px = float(t.entry_price)
        note = (
            f"swing30m_high={t.swing_high_ref:.2f}, swing30m_low={t.swing_low_ref:.2f}, "
            f"breakout={t.breakout_level:.2f}, sl={t.stop_loss:.2f}, target={t.target_price:.2f}, "
            f"points={t.total_points:.2f}, idx(b/r/t/e)=({b_idx}/{r_idx}/{tr_idx}/{e_idx}), "
            f"ref={ref_mode}:{ref_day}"
        )
        ok = send_paper_trade_notification(
            {
                "account": user,
                "symbol": script,
                "action": "SELL" if t.side == "LONG" else "BUY",
                "quantity": qty,
                "price": exit_px,
                "reason": f"SWING_TRAP_{t.exit_reason}",
                "realized_pnl": float(t.total_points),
                "note": note,
                "timestamp": t.exit_ts or t.entry_ts,
            },
            is_entry=False,
        )
        if ok:
            sent += 1
    print(f"[{script}] telegram_sent={sent}/{len(trades)}")


def _parse_scripts(args: argparse.Namespace) -> list[str]:
    raw_items: list[str] = []
    scripts_text = str(getattr(args, "scripts", "") or "")
    if scripts_text.strip():
        raw_items.extend(scripts_text.split(","))

    legacy_script = str(getattr(args, "script", "") or "")
    if legacy_script.strip():
        raw_items.append(legacy_script)

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        s = str(item or "").strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)

    if out:
        return out
    return [str(s).strip().upper() for s in AVAILABLE_SCRIPT_NAMES]


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
    index_mode: bool = False,
) -> list:
    out: list = []
    cur = from_dt
    cont_order = ("0",) if index_mode else ((prefer_cont, "0") if prefer_cont != "0" else ("0",))
    while cur < to_dt:
        end = min(to_dt, cur + timedelta(days=max(1, chunk_days)))
        rows = None
        last_err = None
        for attempt in range(max_retries):
            for cont in cont_order:
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
    ap.add_argument(
        "--script",
        default="",
        help="Legacy single symbol input (deprecated). Prefer --scripts.",
    )
    ap.add_argument(
        "--scripts",
        default="",
        help=(
            "Comma-separated symbols (example: NIFTY,BANKNIFTY,SENSEX). "
            "Empty means all configured scripts."
        ),
    )
    ap.add_argument(
        "--futures",
        action="store_true",
        help="Use nearest futures contract instead of the cash index (NIFTY 50, NIFTY BANK, …).",
    )
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument(
        "--telegram",
        action="store_true",
        help="Send Telegram paper-style notifications for each generated swing-trap trade.",
    )
    ap.add_argument("--csv-out", type=Path, default=REPO_ROOT / "tmp" / "swing_trap_backtest.csv")
    ap.add_argument("--json-out", type=Path, default=REPO_ROOT / "tmp" / "swing_trap_backtest.json")
    args = ap.parse_args()

    creds = load_zerodha_credentials_for_user(args.user)
    api_key = (creds.get("api_key") or "").strip()
    access_token = (creds.get("access_token") or "").strip()
    if not api_key or not access_token:
        raise SystemExit(f"Missing Kite credentials for user={args.user}")

    now = datetime.now(IST)
    from_dt = now - timedelta(days=max(2, int(args.days)))
    to_dt = now

    i1 = map_bot_interval_to_kite("1minute")
    i5 = map_bot_interval_to_kite("5minute")
    i30 = map_bot_interval_to_kite("30minute")

    scripts = _parse_scripts(args)
    cfg = SwingTrapConfig()
    all_trades: list[SwingTrapTrade] = []
    processed: list[tuple[str, str]] = []

    def _df_or_empty(rows: list) -> pd.DataFrame:
        d = kite_candles_to_dataframe(rows)
        return d if d is not None and not d.empty else pd.DataFrame()

    for script in scripts:
        index_mode = not args.futures
        tok: int | None = None
        if index_mode:
            tok = resolve_nse_index_instrument_token(script, api_key, access_token)
        if not tok:
            tok = resolve_kite_instrument_token(script, api_key, access_token)
            index_mode = False
        if not tok:
            print(f"Warning: no Kite instrument token for script={script}; skipping.")
            continue

        try:
            raw1: list = []
            try:
                raw1 = _fetch_chunked(
                    api_key, access_token, tok, i1, from_dt, to_dt, prefer_cont="1", index_mode=index_mode
                )
            except Exception as e:
                print(
                    f"Warning: 1-minute history unavailable for {script} ({e}); "
                    "continuing without 1m trap confirmation."
                )
            raw5 = _fetch_chunked(
                api_key, access_token, tok, i5, from_dt, to_dt, prefer_cont="1", index_mode=index_mode
            )
            raw30 = _fetch_chunked(
                api_key, access_token, tok, i30, from_dt, to_dt, prefer_cont="1", index_mode=index_mode
            )

            df1 = _df_or_empty(raw1)
            df5 = _df_or_empty(raw5)
            df30 = _df_or_empty(raw30)
            trades = run_swing_trap_backtest(df1, df5, df30, cfg)
            for t in trades:
                meta = dict(t.meta or {})
                meta["script"] = script
                t.meta = meta
            all_trades.extend(trades)
            _print_trade_levels_log(script, trades)
            if args.telegram:
                _notify_telegram_for_trades(
                    user=args.user,
                    script=script,
                    trades=trades,
                    qty=float(cfg.total_lots),
                )
            processed.append((script, "index" if index_mode else "futures"))
        except requests.RequestException as e:
            print(f"Warning: Kite API error for {script}, skipping symbol. ({e})")
            continue
        except Exception as e:
            print(f"Warning: backtest failed for {script}, skipping symbol. ({e})")
            continue

    if not all_trades:
        raise SystemExit(
            f"No trades generated for scripts={','.join(scripts)}. "
            "If you saw TokenException or 403, refresh api_key/access_token in "
            f"src/server/data/users/{args.user}/zerodha_credentials.json"
        )

    all_trades.sort(key=lambda t: t.entry_ts)
    trades_to_csv(args.csv_out, all_trades)
    trades_to_json(args.json_out, all_trades)

    total_pts = sum(t.total_points for t in all_trades)
    series_summary = ", ".join(f"{s}:{src}" for s, src in processed)
    print(
        f"Swing/trap backtest | user={args.user} | scripts={','.join(scripts)} | days={args.days} | "
        f"trades={len(all_trades)} | total_points={total_pts:.2f}"
    )
    print(f"Resolved series={series_summary}")
    print(f"CSV={args.csv_out}")
    print(f"JSON={args.json_out}")


if __name__ == "__main__":
    main()
