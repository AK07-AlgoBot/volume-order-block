"""Unified completed-trade store for strategy performance review.

Engines append normalized exit records to Redis (per day). The performance
dashboard reads Redis plus archived Strategy 1 session JSON from the shared
data volume (src/server/data/archive).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.config.paths import archive_dir
from app.services import cache_manager

logger = logging.getLogger("ak07.performance_store")

IST: Final = ZoneInfo("Asia/Kolkata")
ARCHIVE_DIR: Final[Path] = archive_dir()
LEGACY_ARCHIVE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "archive"
ARCHIVE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^performance_review_(\d{4}-\d{2}-\d{2})\.json$")
COMPLETED_TRADES_KEY_TEMPLATE: Final[str] = "ak07:completed_trades:{day}"
TRADE_TTL_SECONDS: Final[int] = 86_400 * 45

STRATEGY_AK07_OI: Final[str] = "Strategy 1 — AK07 OI"
STRATEGY_SMC_CRT: Final[str] = "Strategy 2 — SMC+CRT"
STRATEGY_BREAKOUT: Final[str] = "Strategy 3 — BLR Breakout"
STRATEGY_PRICE_ACTION: Final[str] = "Strategy 4 — Price Action"
STRATEGY_GREEKS: Final[str] = "Strategy 5 — Greeks"
STRATEGY_SR_REVERSAL: Final[str] = "Strategy 6 — S/R Reversal"
STRATEGY_S7_ORB: Final[str] = "Strategy 7 — ORB+"
STRATEGY_CHOCH: Final[str] = "Strategy 8 — CHOCH"
STRATEGY_GAMMA: Final[str] = "Gamma Expiry Observer"
INDEX_ORDER: Final[tuple[str, ...]] = ("NIFTY", "BANKNIFTY", "SENSEX")
STRATEGY_ORDER: Final[tuple[str, ...]] = (
    STRATEGY_AK07_OI,
    STRATEGY_SMC_CRT,
    STRATEGY_BREAKOUT,
    STRATEGY_PRICE_ACTION,
    STRATEGY_GREEKS,
    STRATEGY_SR_REVERSAL,
    STRATEGY_S7_ORB,
    STRATEGY_GAMMA,
)
# Kept for historical trade rows only — not shown in empty strategy summaries.
RETIRED_STRATEGY_LABELS: Final[frozenset[str]] = frozenset({STRATEGY_CHOCH})


def classify_result(pnl_points: float) -> str:
    if pnl_points > 0.01:
        return "WIN"
    if pnl_points < -0.01:
        return "LOSS"
    return "BREAKEVEN"


def _trade_stats(subset: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for t in subset if float(t.get("pnl_points") or 0) > 0.01)
    losses = sum(1 for t in subset if float(t.get("pnl_points") or 0) < -0.01)
    total = len(subset)
    profit = round(sum(float(t.get("pnl_points") or 0) for t in subset), 2)
    win_pct = round(wins / total * 100, 1) if total else 0.0
    return {
        "Trades": total,
        "Wins": wins,
        "Losses": losses,
        "Win %": win_pct,
        "Profit (pts)": profit,
    }


def _ordered_strategies(present: set[str]) -> list[str]:
    ordered = [s for s in STRATEGY_ORDER if s in present]
    ordered.extend(sorted(present - set(STRATEGY_ORDER)))
    return ordered


def _ordered_indices(present: set[str]) -> list[str]:
    ordered = [i for i in INDEX_ORDER if i in present]
    ordered.extend(sorted(present - set(INDEX_ORDER)))
    return ordered


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


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
    username: str = "",
    extra: dict[str, Any] | None = None,
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
    user = str(username or "").strip()
    if user:
        record["username"] = user
    if extra:
        for key, value in extra.items():
            if value is None:
                continue
            if isinstance(value, float):
                record[key] = round(value, 2)
            else:
                record[key] = value
        # Prefer explicit username arg over extra.username when both set.
        if user:
            record["username"] = user
    key = COMPLETED_TRADES_KEY_TEMPLATE.format(day=day)
    existing = cache_manager.get_json(key)
    trades: list[dict[str, Any]] = existing if isinstance(existing, list) else []
    trades.append(record)
    if cache_manager.set_json(key, trades, ttl_seconds=TRADE_TTL_SECONDS):
        logger.info(
            "Recorded %s trade %s %s %+.2f pts (%s)%s",
            strategy,
            symbol,
            direction,
            pnl_points,
            exit_reason,
            f" user={user}" if user else "",
        )


def load_s3_trades(
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """S3 breakout trades only, newest exit first."""
    rows = [
        row
        for row in load_trades(start_date=start_date, end_date=end_date)
        if str(row.get("strategy_id") or "") == "breakout"
        or str(row.get("strategy") or "") == STRATEGY_BREAKOUT
    ]
    rows.sort(key=lambda row: str(row.get("exit_at") or ""), reverse=True)
    return rows


def s3_trade_log_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Admin table rows for Strategy 3."""
    out: list[dict[str, Any]] = []
    for row in trades:
        strike = row.get("option_strike")
        option_type = str(row.get("option_type") or "")
        strike_label = f"{int(strike)}{option_type}" if strike else str(row.get("contract_label") or "—")
        nifty = row.get("spot_entry")
        if nifty is None:
            nifty = row.get("entry_price") if not row.get("option_strike") else None
        entry = row.get("premium_entry")
        if entry is None and row.get("option_strike"):
            entry = row.get("entry_price")
        elif entry is None:
            entry = row.get("entry_price")
        moved = row.get("points_moved")
        if moved is None:
            moved = row.get("premium_points_moved")
        if moved is None and row.get("premium_high") is not None and entry is not None:
            moved = float(row["premium_high"]) - float(entry)
        beyond = row.get("beyond_target")
        if beyond is None and moved is not None and entry is not None:
            try:
                if row.get("tp_price") is not None:
                    beyond = max(0.0, float(moved) - (float(row["tp_price"]) - float(entry)))
                else:
                    beyond = max(0.0, float(moved) - 25.0)
            except (TypeError, ValueError):
                beyond = None
        out.append(
            {
                "Exit at": str(row.get("exit_at") or "")[:19],
                "User": row.get("username") or "—",
                "Nifty": nifty,
                "Strike": strike_label,
                "Direction": row.get("direction") or "",
                "Entry": entry,
                "SL": row.get("sl_price"),
                "Target": row.get("tp_price") or row.get("tp1_price"),
                "Points moved": moved,
                "Beyond target": beyond,
                "Actual pts": row.get("pnl_points"),
                "Result": row.get("result") or classify_result(float(row.get("pnl_points") or 0)),
            }
        )
    return out


def amend_completed_trade(
    day: str,
    *,
    strategy_id: str | None = None,
    symbol: str | None = None,
    trade_index: int = -1,
    entry_price: float | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    exit_at: str | None = None,
) -> dict[str, Any] | None:
    """Update one closed trade row in Redis (operator correction after manual exit)."""
    key = COMPLETED_TRADES_KEY_TEMPLATE.format(day=day)
    rows = cache_manager.get_json(key)
    if not isinstance(rows, list) or not rows:
        return None

    matches = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if strategy_id and str(row.get("strategy_id") or "") != strategy_id:
            continue
        if symbol and str(row.get("symbol") or "").upper() != symbol.upper():
            continue
        matches.append(i)

    if not matches:
        return None

    idx = matches[trade_index] if -len(matches) <= trade_index < len(matches) else matches[-1]
    row = dict(rows[idx])
    direction = str(row.get("direction") or "LONG")

    if entry_price is not None:
        row["entry_price"] = round(float(entry_price), 2)
    if exit_price is not None:
        row["exit_price"] = round(float(exit_price), 2)
    if exit_reason is not None:
        row["exit_reason"] = exit_reason
    if exit_at is not None:
        row["exit_at"] = exit_at

    entry = float(row.get("entry_price") or 0)
    exit_p = float(row.get("exit_price") or 0)
    pnl = (exit_p - entry) if direction == "LONG" else (entry - exit_p)
    row["pnl_points"] = round(pnl, 2)
    row["result"] = classify_result(pnl)

    rows[idx] = row
    if cache_manager.set_json(key, rows, ttl_seconds=TRADE_TTL_SECONDS):
        logger.info(
            "Amended trade %s [%s] %s exit=%.2f pnl=%+.2f (%s)",
            day,
            idx,
            symbol or row.get("symbol"),
            exit_p,
            pnl,
            row.get("exit_reason"),
        )
        return row
    return None


def list_completed_trades(day: str, *, strategy_id: str | None = None) -> list[dict[str, Any]]:
    key = COMPLETED_TRADES_KEY_TEMPLATE.format(day=day)
    rows = cache_manager.get_json(key)
    if not isinstance(rows, list):
        return []
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if strategy_id and str(row.get("strategy_id") or "") != strategy_id:
            continue
        out.append({"index": i, **row})
    return out


def ingest_strategy1_trade_log(
    day: str,
    trade_log: list[dict[str, Any]],
    *,
    paper_trading: bool,
) -> int:
    """Import EXIT / PARTIAL_BOOK rows from a Strategy 1 archive into Redis."""
    parsed = _parse_archive_payload(day, trade_log, paper_trading)
    if not parsed:
        return 0

    key = COMPLETED_TRADES_KEY_TEMPLATE.format(day=day)
    existing = cache_manager.get_json(key)
    merged: list[dict[str, Any]] = existing if isinstance(existing, list) else []
    seen = {_fingerprint(row) for row in merged}
    added = 0
    for row in parsed:
        fp = _fingerprint(row)
        if fp in seen:
            continue
        seen.add(fp)
        merged.append(row)
        added += 1
    if added:
        cache_manager.set_json(key, merged, ttl_seconds=TRADE_TTL_SECONDS)
        logger.info("Ingested %d Strategy 1 closed trade(s) for %s into Redis", added, day)
    return added


def migrate_legacy_archives_to_volume(*, ingest_redis: bool = True) -> dict[str, Any]:
    """Copy performance_review_*.json from legacy app/archive into the data volume."""
    import shutil

    target = ARCHIVE_DIR
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    ingested_days: list[str] = []

    if not LEGACY_ARCHIVE_DIR.is_dir():
        return {
            "legacy_dir": str(LEGACY_ARCHIVE_DIR),
            "target_dir": str(target),
            "copied": copied,
            "ingested_days": ingested_days,
        }

    for path in sorted(LEGACY_ARCHIVE_DIR.glob("performance_review_*.json")):
        if not ARCHIVE_NAME_RE.match(path.name):
            continue
        dest = target / path.name
        if not dest.exists() or dest.stat().st_size < path.stat().st_size:
            shutil.copy2(path, dest)
            copied.append(path.name)
        if ingest_redis:
            try:
                payload = json.loads(dest.read_text(encoding="utf-8"))
                day = str(payload.get("date") or dest.stem.replace("performance_review_", ""))
                trade_log = payload.get("trade_log") or []
                if isinstance(trade_log, list):
                    added = ingest_strategy1_trade_log(
                        day,
                        trade_log,
                        paper_trading=bool(payload.get("paper_trading", True)),
                    )
                    if added:
                        ingested_days.append(day)
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                logger.warning("Could not ingest %s after migrate: %s", path.name, exc)

    if copied:
        logger.info(
            "Migrated %d archive file(s) from %s -> %s",
            len(copied),
            LEGACY_ARCHIVE_DIR,
            target,
        )
    return {
        "legacy_dir": str(LEGACY_ARCHIVE_DIR),
        "target_dir": str(target),
        "copied": copied,
        "ingested_days": ingested_days,
    }


def _parse_archive_payload(
    day: str,
    trade_log: list[Any],
    paper_trading: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in trade_log:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event") or "")
        if event_type not in ("EXIT", "PARTIAL_BOOK"):
            continue
        points = event.get("points")
        if points is None:
            continue
        pnl = float(points)
        reason = str(event.get("reason") or event_type)
        if event_type == "PARTIAL_BOOK" and not reason.startswith("PARTIAL_BOOK"):
            reason = f"PARTIAL_BOOK — {reason}"
        elif event_type == "EXIT" and reason in ("STOP_LOSS", "TARGET", "TIME_GATE_1455", "KILL_SWITCH"):
            reason = reason
        out.append(
            {
                "strategy": STRATEGY_AK07_OI,
                "strategy_id": "ak07_oi",
                "symbol": str(event.get("index") or ""),
                "direction": str(event.get("direction") or ""),
                "entry_price": float(
                    event.get("entry_spot") or event.get("exit_spot") or event.get("spot") or 0
                ),
                "exit_price": float(event.get("exit_spot") or event.get("spot") or 0),
                "pnl_points": round(pnl, 2),
                "result": classify_result(pnl),
                "exit_reason": reason,
                "entry_at": "",
                "exit_at": str(event.get("at") or f"{day}T15:30:00+05:30"),
                "paper_trading": paper_trading,
            }
        )
    return out


def _parse_archive_trades(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []

    day = str(payload.get("date") or path.stem.replace("performance_review_", ""))
    paper = bool(payload.get("paper_trading", True))
    trade_log = payload.get("trade_log") or []
    if not isinstance(trade_log, list):
        return []
    rows = _parse_archive_payload(day, trade_log, paper)
    if rows:
        return rows
    # Fallback when trade_log only has ENTRY rows but session PnL was recorded.
    pnl_by_index = payload.get("pnl_points_by_index") or {}
    if not isinstance(pnl_by_index, dict):
        return []
    for index, raw_pnl in pnl_by_index.items():
        pnl = float(raw_pnl or 0)
        if abs(pnl) < 0.01:
            continue
        rows.append(
            {
                "strategy": STRATEGY_AK07_OI,
                "strategy_id": "ak07_oi",
                "symbol": str(index),
                "direction": "",
                "entry_price": 0.0,
                "exit_price": 0.0,
                "pnl_points": round(pnl, 2),
                "result": classify_result(pnl),
                "exit_reason": "session_archive_summary",
                "entry_at": "",
                "exit_at": f"{day}T15:30:00+05:30",
                "paper_trading": paper,
            }
        )
    return rows


def _archive_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    for candidate in (ARCHIVE_DIR, LEGACY_ARCHIVE_DIR):
        if candidate in dirs:
            continue
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def list_archive_files() -> list[Path]:
    """All performance_review_*.json files on disk (newest first)."""
    seen_names: set[str] = set()
    files: list[Path] = []
    for directory in _archive_search_dirs():
        for path in directory.glob("performance_review_*.json"):
            if not path.is_file() or not ARCHIVE_NAME_RE.match(path.name):
                continue
            if path.name in seen_names:
                continue
            seen_names.add(path.name)
            files.append(path)
    files.sort(key=lambda p: p.name, reverse=True)
    return files


def archive_dates() -> list[str]:
    return [
        match.group(1)
        for path in list_archive_files()
        if (match := ARCHIVE_NAME_RE.match(path.name))
    ]


def load_status(start_date: date, end_date: date) -> dict[str, Any]:
    """Diagnostics for the performance dashboard."""
    archive_files = list_archive_files()
    archive_in_range = 0
    for path in archive_files:
        match = ARCHIVE_NAME_RE.match(path.name)
        if not match:
            continue
        day = date.fromisoformat(match.group(1))
        if start_date <= day <= end_date:
            archive_in_range += 1

    redis_days = 0
    cursor = start_date
    while cursor <= end_date:
        key = COMPLETED_TRADES_KEY_TEMPLATE.format(day=cursor.isoformat())
        rows = cache_manager.get_json(key)
        if isinstance(rows, list) and rows:
            redis_days += 1
        cursor += timedelta(days=1)

    return {
        "archive_dir": str(ARCHIVE_DIR),
        "archive_dir_exists": ARCHIVE_DIR.is_dir(),
        "legacy_archive_dir": str(LEGACY_ARCHIVE_DIR),
        "legacy_archive_exists": LEGACY_ARCHIVE_DIR.is_dir(),
        "archive_files_total": len(archive_files),
        "archive_files_in_range": archive_in_range,
        "redis_days_with_trades": redis_days,
        "latest_archive": archive_files[0].name if archive_files else None,
    }


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
    else:
        legacy_path = LEGACY_ARCHIVE_DIR / f"performance_review_{day}.json"
        if legacy_path.is_file():
            for row in _parse_archive_trades(legacy_path):
                fp = _fingerprint(row)
                if fp in seen:
                    continue
                seen.add(fp)
                out.append(row)

    return out


def _fingerprint(row: dict[str, Any]) -> str:
    """Dedupe live Redis rows vs archive ingest (same fill, different reason text)."""
    exit_at = str(row.get("exit_at") or "")
    if len(exit_at) >= 19:
        exit_at = exit_at[:19]
    return "|".join(
        [
            str(row.get("strategy_id") or row.get("strategy")),
            str(row.get("symbol")),
            str(row.get("direction")),
            str(row.get("username") or ""),
            exit_at,
            str(row.get("pnl_points")),
        ]
    )


def load_trades(
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Load completed trades from Redis + archive files for the inclusive date range."""
    migrate_legacy_archives_to_volume(ingest_redis=True)

    if end_date is None:
        end_date = datetime.now(IST).date()
    if start_date is None:
        start_date = end_date - timedelta(days=30)

    seen: set[str] = set()
    trades: list[dict[str, Any]] = []

    # Redis + exact-day archive files
    cursor = start_date
    while cursor <= end_date:
        trades.extend(_load_day_trades(cursor.isoformat(), seen))
        cursor += timedelta(days=1)

    # Any archive file in range (covers files whose date key was missed)
    for path in list_archive_files():
        match = ARCHIVE_NAME_RE.match(path.name)
        if not match:
            continue
        day = date.fromisoformat(match.group(1))
        if day < start_date or day > end_date:
            continue
        for row in _parse_archive_trades(path):
            fp = _fingerprint(row)
            if fp in seen:
                continue
            seen.add(fp)
            trades.append(row)

    trades.sort(key=lambda row: str(row.get("exit_at") or ""))
    return trades


def summarize_by_strategy(
    trades: list[dict[str, Any]],
    *,
    allowed_strategies: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate stats per strategy plus a TOTAL row.

    When ``allowed_strategies`` is set (non-admin Performance Review), only those
    strategy labels are shown — never the full STRATEGY_ORDER catalog.
    """
    rows: list[dict[str, Any]] = []
    grand_trades = grand_wins = grand_losses = 0
    grand_profit = 0.0

    present = {str(t.get("strategy")) for t in trades}
    if allowed_strategies is not None:
        allowed = [s for s in allowed_strategies if s]
        ordered = [s for s in allowed if s in present]
        ordered.extend([s for s in allowed if s not in present])
    else:
        ordered = _ordered_strategies(present)

    for strategy in ordered:
        subset = [t for t in trades if str(t.get("strategy")) == strategy]
        stats = _trade_stats(subset)
        rows.append({"Strategy": strategy, **stats})
        grand_trades += int(stats["Trades"])
        grand_wins += int(stats["Wins"])
        grand_losses += int(stats["Losses"])
        grand_profit += float(stats["Profit (pts)"])

    if not rows:
        fill = list(allowed_strategies) if allowed_strategies is not None else list(STRATEGY_ORDER)
        for strategy in fill:
            rows.append({"Strategy": strategy, **_trade_stats([])})

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


def summarize_by_index(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate stats per index plus a TOTAL row."""
    rows: list[dict[str, Any]] = []
    grand_trades = grand_wins = grand_losses = 0
    grand_profit = 0.0

    present = {_normalize_symbol(t.get("symbol")) for t in trades if _normalize_symbol(t.get("symbol"))}
    for index in _ordered_indices(present):
        subset = [t for t in trades if _normalize_symbol(t.get("symbol")) == index]
        stats = _trade_stats(subset)
        rows.append({"Index": index, **stats})
        grand_trades += int(stats["Trades"])
        grand_wins += int(stats["Wins"])
        grand_losses += int(stats["Losses"])
        grand_profit += float(stats["Profit (pts)"])

    if not rows and not trades:
        return []

    rows.append(
        {
            "Index": "TOTAL",
            "Trades": grand_trades,
            "Wins": grand_wins,
            "Losses": grand_losses,
            "Win %": round(grand_wins / grand_trades * 100, 1) if grand_trades else 0.0,
            "Profit (pts)": round(grand_profit, 2),
        }
    )
    return rows


def summarize_by_strategy_and_index(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per strategy × index with trades, win rate, and profit."""
    rows: list[dict[str, Any]] = []
    strategies = _ordered_strategies({str(t.get("strategy")) for t in trades})
    indices = _ordered_indices(
        {_normalize_symbol(t.get("symbol")) for t in trades if _normalize_symbol(t.get("symbol"))}
    )
    if not strategies or not indices:
        return rows

    for strategy in strategies:
        for index in indices:
            subset = [
                t
                for t in trades
                if str(t.get("strategy")) == strategy and _normalize_symbol(t.get("symbol")) == index
            ]
            if not subset:
                continue
            stats = _trade_stats(subset)
            rows.append({"Strategy": strategy, "Index": index, **stats})

    rows.sort(
        key=lambda row: (
            _ordered_strategies({str(row.get("Strategy"))}).index(str(row.get("Strategy"))),
            _ordered_indices({str(row.get("Index"))}).index(str(row.get("Index"))),
        )
    )
    return rows


def build_day_summary(day: str) -> dict[str, Any]:
    """Unified closed-trade summary for one session day (Redis + archive)."""
    trades = _load_day_trades(day, set())
    by_strategy = summarize_by_strategy(trades)
    by_index = summarize_by_index(trades)
    by_strategy_index = summarize_by_strategy_and_index(trades)
    total_row = by_strategy[-1] if by_strategy else _trade_stats([])
    return {
        "day": day,
        "trade_count": int(total_row.get("Trades") or 0),
        "wins": int(total_row.get("Wins") or 0),
        "losses": int(total_row.get("Losses") or 0),
        "win_pct": float(total_row.get("Win %") or 0.0),
        "pnl_points_total": float(total_row.get("Profit (pts)") or 0.0),
        "by_strategy": by_strategy,
        "by_index": by_index,
        "by_strategy_and_index": by_strategy_index,
        "trades": trades,
    }


def format_day_summary_telegram(
    archive_path: Path | str,
    *,
    day: str,
    s1_pnl_by_index: dict[str, float] | None = None,
    s1_event_count: int = 0,
) -> str:
    """Human-readable 15:30 archival message with all-strategy and index splits."""
    summary = build_day_summary(day)
    total = float(summary["pnl_points_total"])
    count = int(summary["trade_count"])
    wins = int(summary["wins"])
    losses = int(summary["losses"])

    lines = [
        f"Session archived to `{archive_path}`",
        f"All strategies: **{total:+.2f} pts** · **{count}** closed trade(s) ({wins}W/{losses}L)",
    ]

    s1_total = round(sum((s1_pnl_by_index or {}).values()), 2)
    if s1_event_count or abs(s1_total) >= 0.01:
        lines.append(f"S1 session log: {s1_total:+.2f} pts · {s1_event_count} event(s)")

    by_strategy = [row for row in summary["by_strategy"] if row.get("Strategy") != "TOTAL"]
    active_strategies = [row for row in by_strategy if int(row.get("Trades") or 0) > 0]
    if active_strategies:
        lines.append("")
        lines.append("By strategy:")
        for row in active_strategies:
            lines.append(
                f"• {row['Strategy']}: {float(row['Profit (pts)']):+.2f} pts · "
                f"{int(row['Trades'])} trade(s) ({int(row['Wins'])}W/{int(row['Losses'])}L)"
            )

    by_index = [row for row in summary["by_index"] if row.get("Index") != "TOTAL"]
    active_indices = [row for row in by_index if int(row.get("Trades") or 0) > 0]
    if active_indices:
        lines.append("")
        lines.append("By index:")
        for row in active_indices:
            lines.append(
                f"• {row['Index']}: {float(row['Profit (pts)']):+.2f} pts · "
                f"{int(row['Trades'])} trade(s) ({int(row['Wins'])}W/{int(row['Losses'])}L)"
            )

    matrix = summary["by_strategy_and_index"]
    if matrix:
        lines.append("")
        lines.append("Strategy × index (pts):")
        for row in matrix:
            lines.append(
                f"• {row['Strategy']} / {row['Index']}: "
                f"{float(row['Profit (pts)']):+.2f} pts · {int(row['Trades'])} trade(s)"
            )

    return "\n".join(lines)


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


def classify_loss_reason(exit_reason: str, strategy: str) -> str:
    """Human-readable bucket for loss post-mortems."""
    reason = (exit_reason or "").upper()
    strat = strategy or ""
    if "STOP_LOSS" in reason or reason == "SL HIT" or " SL" in f" {reason}":
        return "Stop-loss hit"
    if "SQUARE_OFF" in reason or "TIME_GATE" in reason or "1455" in reason:
        return "Intraday square-off (14:55)"
    if "KILL_SWITCH" in reason:
        return "Kill switch"
    if "TP1" in reason or "TARGET" in reason or "PARTIAL" in reason:
        return "Target/partial (not a loss unless mis-tagged)"
    if "Strategy 1" in strat or "ak07_oi" in reason.lower():
        if "STOP" in reason:
            return "S1 OI stop (check SL pts vs 30/60 rule)"
    if "Strategy 3" in strat or "breakout" in reason.lower():
        return "S3 BLR — structural SL or fixed TP miss"
    if "Strategy 6" in strat or "sr_reversal" in reason.lower():
        return "S6 S/R — zone fade failed"
    return "Other exit"


def analyze_losses(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize losing trades for Performance Review."""
    losses = [t for t in trades if float(t.get("pnl_points") or 0) < -0.01]
    wins = [t for t in trades if float(t.get("pnl_points") or 0) > 0.01]

    by_strategy: dict[str, list[dict[str, Any]]] = {}
    by_bucket: dict[str, int] = {}
    by_day: dict[str, float] = {}
    rows: list[dict[str, Any]] = []

    filter_exempt = {
        STRATEGY_AK07_OI,
    }

    for t in losses:
        strat = str(t.get("strategy") or "")
        by_strategy.setdefault(strat, []).append(t)
        bucket = classify_loss_reason(str(t.get("exit_reason") or ""), strat)
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
        day = str(t.get("exit_at") or "")[:10]
        by_day[day] = by_day.get(day, 0.0) + float(t.get("pnl_points") or 0)
        rows.append(
            {
                "date": day,
                "strategy": strat,
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "pnl_pts": float(t.get("pnl_points") or 0),
                "exit_reason": t.get("exit_reason"),
                "loss_bucket": bucket,
                "blr_filter": "exempt" if strat in filter_exempt else "S3 day review",
                "paper": bool(t.get("paper_trading")),
            }
        )

    strategy_summary = [
        {
            "Strategy": s,
            "Losses": len(items),
            "Loss pts": round(sum(float(x.get("pnl_points") or 0) for x in items), 2),
        }
        for s, items in sorted(by_strategy.items())
    ]

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "loss_rows": sorted(rows, key=lambda r: str(r.get("date") or ""), reverse=True),
        "by_bucket": by_bucket,
        "by_strategy": strategy_summary,
        "worst_days": sorted(by_day.items(), key=lambda x: x[1])[:5],
        "filter_note": (
            "S2/S3/S4/S5 only take trades aligned with S3 day review (9:20 5m close vs Mid). "
            "S1 and S6 are exempt — losses there are not filter failures."
        ),
    }


def purge_trades_for_day(
    day: str,
    *,
    paper_only: bool = True,
    remove_archive: bool = True,
    remove_trade_log: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove paper/simulation trades for one session day from Redis and archives."""
    result: dict[str, Any] = {
        "day": day,
        "redis_removed": 0,
        "redis_remaining": 0,
        "archive_removed": False,
        "archive_path": "",
        "trade_log_removed": False,
        "dry_run": dry_run,
    }

    key = COMPLETED_TRADES_KEY_TEMPLATE.format(day=day)
    rows = cache_manager.get_json(key)
    if not isinstance(rows, list):
        rows = []

    if paper_only:
        kept = [r for r in rows if isinstance(r, dict) and not r.get("paper_trading")]
        removed = len(rows) - len(kept)
    else:
        kept = []
        removed = len(rows)

    result["redis_removed"] = removed
    result["redis_remaining"] = len(kept)

    if not dry_run:
        if kept:
            cache_manager.set_json(key, kept, ttl_seconds=TRADE_TTL_SECONDS)
        elif rows:
            cache_manager.delete_key(key)

    if remove_archive:
        for directory in _archive_search_dirs():
            path = directory / f"performance_review_{day}.json"
            if not path.is_file():
                continue
            result["archive_path"] = str(path)
            delete_archive = True
            if paper_only:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    delete_archive = bool(payload.get("paper_trading", True))
                except (OSError, json.JSONDecodeError):
                    delete_archive = True
            if delete_archive:
                result["archive_removed"] = True
                if not dry_run:
                    path.unlink(missing_ok=True)
            break

    if remove_trade_log and (result["redis_removed"] or result["archive_removed"]):
        result["trade_log_removed"] = True
        if not dry_run:
            cache_manager.delete_key(cache_manager.TRADE_LOG_KEY_TEMPLATE.format(day=day))

    if result["redis_removed"] or result["archive_removed"]:
        logger.info(
            "Purged %s: redis -%d (kept %d), archive %s, trade_log %s",
            day,
            result["redis_removed"],
            result["redis_remaining"],
            "removed" if result["archive_removed"] else "unchanged",
            "removed" if result["trade_log_removed"] else "unchanged",
        )

    return result


def purge_trades_for_days(
    days: list[str],
    *,
    paper_only: bool = True,
    remove_archive: bool = True,
    remove_trade_log: bool = True,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    return [
        purge_trades_for_day(
            day,
            paper_only=paper_only,
            remove_archive=remove_archive,
            remove_trade_log=remove_trade_log,
            dry_run=dry_run,
        )
        for day in days
    ]
