"""Unified completed-trade store for strategy performance review.

Engines append normalized exit records to Redis (per day). The performance
dashboard also reads archived Strategy 1 session JSON when present.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.services import cache_manager

logger = logging.getLogger("ak07.performance_store")

IST: Final = ZoneInfo("Asia/Kolkata")
ARCHIVE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "archive"
COMPLETED_TRADES_KEY_TEMPLATE: Final[str] = "ak07:completed_trades:{day}"
TRADE_TTL_SECONDS: Final[int] = 86_400 * 45

STRATEGY_AK07_OI: Final[str] = "Strategy 1 — AK07 OI"
STRATEGY_SMC_CRT: Final[str] = "Strategy 2 — SMC+CRT"
STRATEGY_BREAKOUT: Final[str] = "Strategy 3 — BLR Breakout"

STRATEGY_ORDER: Final[tuple[str, ...]] = (
    STRATEGY_AK07_OI,
    STRATEGY_SMC_CRT,
    STRATEGY_BREAKOUT,
)


def classify_result(pnl_points: float) -> str:
    if pnl_points > 0.01:
        return "WIN"
    if pnl_points < -0.01:
        return "LOSS"
    return "BREAKEVEN"


def record_completed_trade(
    *,
    strategy: str,
    strategy_id: str,
    symbol: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    pnl_points: float,
    exit_reason: str,
    entry_at: str = "",
    exit_at: str | None = None,
    paper_trading: bool = True,
) -> None:
    """Append one closed trade to the day bucket in Redis (fail-safe)."""
    now = datetime.now(IST)
    day = now.date().isoformat()
    record: dict[str, Any] = {
        "strategy": strategy,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "direction": direction,
        "entry_price": round(float(entry_price), 2),
        "exit_price": round(float(exit_price), 2),
        "pnl_points": round(float(pnl_points), 2),
        "result": classify_result(pnl_points),
        "exit_reason": exit_reason,
        "entry_at": entry_at or "",
        "exit_at": exit_at or now.isoformat(),
        "paper_trading": paper_trading,
    }
    key = COMPLETED_TRADES_KEY_TEMPLATE.format(day=day)
    existing = cache_manager.get_json(key)
    trades: list[dict[str, Any]] = existing if isinstance(existing, list) else []
    trades.append(record)
    if cache_manager.set_json(key, trades, ttl_seconds=TRADE_TTL_SECONDS):
        logger.info(
            "Recorded %s trade %s %s %+.2f pts (%s)",
            strategy,
            symbol,
            direction,
            pnl_points,
            exit_reason,
        )


def _parse_archive_trades(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []

    day = str(payload.get("date") or path.stem.replace("performance_review_", ""))
    paper = bool(payload.get("paper_trading", True))
    out: list[dict[str, Any]] = []
    for event in payload.get("trade_log") or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event") or "")
        if event_type not in ("EXIT", "PARTIAL_BOOK"):
            continue
        points = event.get("points")
        if points is None:
            continue
        pnl = float(points)
        out.append(
            {
                "strategy": STRATEGY_AK07_OI,
                "strategy_id": "ak07_oi",
                "symbol": str(event.get("index") or ""),
                "direction": str(event.get("direction") or ""),
                "entry_price": float(event.get("entry_spot") or event.get("spot") or 0),
                "exit_price": float(event.get("exit_spot") or event.get("spot") or 0),
                "pnl_points": round(pnl, 2),
                "result": classify_result(pnl),
                "exit_reason": str(event.get("reason") or event_type),
                "entry_at": "",
                "exit_at": str(event.get("at") or f"{day}T15:30:00+05:30"),
                "paper_trading": paper,
            }
        )
    return out


def _load_day_trades(day: str, seen: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    key = COMPLETED_TRADES_KEY_TEMPLATE.format(day=day)
    redis_rows = cache_manager.get_json(key)
    if isinstance(redis_rows, list):
        for row in redis_rows:
            if not isinstance(row, dict):
                continue
            fp = _fingerprint(row)
            if fp in seen:
                continue
            seen.add(fp)
            out.append(row)

    archive_path = ARCHIVE_DIR / f"performance_review_{day}.json"
    if archive_path.is_file():
        for row in _parse_archive_trades(archive_path):
            fp = _fingerprint(row)
            if fp in seen:
                continue
            seen.add(fp)
            out.append(row)

    return out


def _fingerprint(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("strategy_id") or row.get("strategy")),
            str(row.get("symbol")),
            str(row.get("exit_at")),
            str(row.get("pnl_points")),
            str(row.get("exit_reason")),
        ]
    )


def load_trades(
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Load completed trades from Redis + archive files for the inclusive date range."""
    if end_date is None:
        end_date = datetime.now(IST).date()
    if start_date is None:
        start_date = end_date - timedelta(days=30)

    seen: set[str] = set()
    trades: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        trades.extend(_load_day_trades(cursor.isoformat(), seen))
        cursor += timedelta(days=1)

    trades.sort(key=lambda row: str(row.get("exit_at") or ""))
    return trades


def summarize_by_strategy(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate stats per strategy plus a TOTAL row."""
    rows: list[dict[str, Any]] = []
    grand_trades = grand_wins = grand_losses = 0
    grand_profit = 0.0

    present = {str(t.get("strategy")) for t in trades}
    ordered = [s for s in STRATEGY_ORDER if s in present]
    ordered.extend(sorted(present - set(STRATEGY_ORDER)))

    for strategy in ordered:
        subset = [t for t in trades if str(t.get("strategy")) == strategy]
        wins = sum(1 for t in subset if float(t.get("pnl_points") or 0) > 0.01)
        losses = sum(1 for t in subset if float(t.get("pnl_points") or 0) < -0.01)
        total = len(subset)
        profit = round(sum(float(t.get("pnl_points") or 0) for t in subset), 2)
        win_pct = round(wins / total * 100, 1) if total else 0.0
        rows.append(
            {
                "Strategy": strategy,
                "Trades": total,
                "Wins": wins,
                "Losses": losses,
                "Win %": win_pct,
                "Profit (pts)": profit,
            }
        )
        grand_trades += total
        grand_wins += wins
        grand_losses += losses
        grand_profit += profit

    if not rows:
        for strategy in STRATEGY_ORDER:
            rows.append(
                {
                    "Strategy": strategy,
                    "Trades": 0,
                    "Wins": 0,
                    "Losses": 0,
                    "Win %": 0.0,
                    "Profit (pts)": 0.0,
                }
            )

    rows.append(
        {
            "Strategy": "TOTAL",
            "Trades": grand_trades,
            "Wins": grand_wins,
            "Losses": grand_losses,
            "Win %": round(grand_wins / grand_trades * 100, 1) if grand_trades else 0.0,
            "Profit (pts)": round(grand_profit, 2),
        }
    )
    return rows


def daily_pnl_series(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Daily summed PnL for charting."""
    buckets: dict[str, float] = {}
    for trade in trades:
        exit_at = str(trade.get("exit_at") or "")
        day = exit_at[:10] if len(exit_at) >= 10 else "unknown"
        buckets[day] = buckets.get(day, 0.0) + float(trade.get("pnl_points") or 0)

    cumulative = 0.0
    series: list[dict[str, Any]] = []
    for day in sorted(buckets):
        cumulative += buckets[day]
        series.append(
            {
                "date": day,
                "daily_pnl": round(buckets[day], 2),
                "cumulative_pnl": round(cumulative, 2),
            }
        )
    return series
