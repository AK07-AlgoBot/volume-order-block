import asyncio
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import re
from typing import List, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from bot_process_control import restart_trading_bot_after_credential_save
from upstox_credentials_store import (
    CREDENTIALS_FILE,
    mask_tail,
    normalize_access_token,
    persist_credentials,
    read_credentials_file,
)


_cors_raw = os.environ.get(
    "DASHBOARD_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()] or [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app = FastAPI(title="AK07 Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Trade(BaseModel):
    id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: float
    entry_price: float | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    chart_percent: float | None = None
    win_percent: float | None = None
    exit_price: float | None = None
    last_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    opened_at: str
    closed_at: str | None = None


class WeeklyPnlPoint(BaseModel):
    date: str
    pnl: float


class UpstoxSettingsBody(BaseModel):
    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = ""


def _require_dashboard_admin(request: Request) -> None:
    expected = os.environ.get("DASHBOARD_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    got = (request.headers.get("X-Dashboard-Admin-Token") or "").strip()
    if got != expected:
        raise HTTPException(
            status_code=401,
            detail="Set header X-Dashboard-Admin-Token to match DASHBOARD_ADMIN_TOKEN on the server.",
        )


live_trades: dict[str, dict] = {}
closed_trades: list[dict] = []
ws_clients: List[WebSocket] = []
ORDER_LOG_FILE = Path("orders.log")
ARCHIVE_ROOT = Path("archive")

LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
    "CRUDE": 100,
    "GOLDMINI": 1,
    "SILVERMINI": 5,
}

LINE_PATTERN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - "
    r"(?P<script>[^|]+) \| ACTION=(?P<action>ENTRY|EXIT) \| SIDE=(?P<side>BUY|SELL) "
    r"\| PRICE=(?P<price>\d+(?:\.\d+)?)"
)
ORDER_ID_PATTERN = re.compile(r"order_id=(?P<order_id>\d+)")


def _sort_closed_trades() -> None:
    closed_trades.sort(
        key=lambda t: t.get("closed_at") or "",
        reverse=True,
    )


async def _broadcast(message: dict) -> None:
    stale_clients = []
    for ws in ws_clients:
        try:
            await ws.send_json(message)
        except Exception:
            stale_clients.append(ws)

    for ws in stale_clients:
        if ws in ws_clients:
            ws_clients.remove(ws)


def _parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")


def _week_monday_to_friday(week_offset: int = 0) -> list[datetime.date]:
    """
    Return Monday-Friday dates for a week.
    week_offset=0 => current week, 1 => previous week, etc.
    """
    today = datetime.now().date()
    base_monday = today - timedelta(days=today.weekday())
    monday = base_monday - timedelta(days=7 * max(0, week_offset))
    return [monday + timedelta(days=day_offset) for day_offset in range(5)]


def _get_order_log_files() -> list[Path]:
    files = []
    if ORDER_LOG_FILE.exists():
        files.append(ORDER_LOG_FILE)

    if ARCHIVE_ROOT.exists():
        files.extend(sorted(ARCHIVE_ROOT.glob("*/logs/orders.log")))

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for file_path in files:
        resolved = str(file_path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(file_path)
    return deduped


def _parse_order_events() -> list[tuple]:
    """
    Parse ENTRY/EXIT events from active + archived order logs and dedupe them.

    Multiple archive snapshots can contain overlapping lines. Without dedupe,
    FIFO entry/exit pairing may drift and produce incorrect closed trades.
    """
    order_files = _get_order_log_files()
    if not order_files:
        return []

    parsed_events = []
    for order_file in order_files:
        lines = order_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        for raw in lines:
            stripped = raw.strip()
            match = LINE_PATTERN.search(stripped)
            if not match:
                continue
            order_id_match = ORDER_ID_PATTERN.search(stripped)
            order_id = order_id_match.group("order_id") if order_id_match else ""
            parsed_events.append(
                (
                    _parse_ts(match.group("ts")),
                    match.group("script").strip(),
                    match.group("action"),
                    match.group("side"),
                    float(match.group("price")),
                    order_id,
                )
            )

    parsed_events.sort(key=lambda event: event[0])

    # Global dedupe across current + archive snapshots.
    deduped = []
    seen = set()
    for event in parsed_events:
        key = (
            event[0].isoformat(timespec="milliseconds"),
            event[1],  # script
            event[2],  # action
            event[3],  # side
            round(float(event[4]), 4),  # price
            event[5],  # order_id (may be empty)
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)

    return deduped


def _pop_matching_entry(entry_queue: deque, exit_side: str) -> dict | None:
    """
    Pop the latest entry that can legally be closed by this exit side.
    EXIT SELL must close BUY entry, EXIT BUY must close SELL entry.

    We intentionally prefer latest-match (LIFO per side) because archive
    gaps can leave stale unmatched entries from much older sessions; pairing
    exits with the nearest valid prior entry prevents cross-day drift.
    """
    expected_entry_side = "BUY" if exit_side == "SELL" else "SELL"
    for idx in range(len(entry_queue) - 1, -1, -1):
        entry = entry_queue[idx]
        if str(entry.get("side")) == expected_entry_side:
            if idx == 0:
                return entry_queue.popleft()
            if idx == len(entry_queue) - 1:
                return entry_queue.pop()
            # deque has no pop(idx) for middle indices; remove by value occurrence.
            selected = entry
            entry_queue.remove(selected)
            return selected
    return None


def _compute_weekly_pnl_from_orders(week_offset: int = 0) -> list[dict]:
    """Compute realized P&L for selected week's Monday-Friday from active+archived order logs."""
    weekdays = _week_monday_to_friday(week_offset)
    weekday_set = set(weekdays)
    pnl_by_date = {day: 0.0 for day in weekdays}

    parsed_events = _parse_order_events()
    if not parsed_events:
        return [
            {"date": day.strftime("%Y-%m-%d"), "pnl": 0.0}
            for day in weekdays
        ]
    entries = defaultdict(deque)

    for ts, script, action, side, price, _order_id in parsed_events:

        if action == "ENTRY":
            entries[script].append({"side": side, "price": price, "ts": ts})
            continue

        if not entries[script]:
            continue

        entry = _pop_matching_entry(entries[script], side)
        if entry is None:
            continue
        lot = LOT_SIZES.get(script, 1)

        if entry["side"] == "BUY" and side == "SELL":
            points = price - entry["price"]
        elif entry["side"] == "SELL" and side == "BUY":
            points = entry["price"] - price
        else:
            points = 0.0

        trade_day = ts.date()
        # Ignore weekends entirely and include only Mon-Fri window
        if trade_day in weekday_set:
            pnl_by_date[trade_day] += points * lot

    return [
        {"date": day.strftime("%Y-%m-%d"), "pnl": round(pnl_by_date[day], 2)}
        for day in weekdays
    ]


def _weekly_total(points: list[dict]) -> float:
    return round(sum(float(point.get("pnl", 0.0) or 0.0) for point in points), 2)


def _weekly_filter_options(count: int = 12) -> list[dict]:
    options = []
    for offset in range(max(1, count)):
        weekdays = _week_monday_to_friday(offset)
        start_day = weekdays[0].strftime("%Y-%m-%d")
        end_day = weekdays[-1].strftime("%Y-%m-%d")
        label = "Current Week" if offset == 0 else f"{offset} Week Ago"
        options.append(
            {
                "week_offset": offset,
                "label": label,
                "range_start": start_day,
                "range_end": end_day,
            }
        )
    return options


def _build_closed_trades_from_orders(limit: int = 300) -> list[dict]:
    """Reconstruct closed trades from order logs (active + archived)."""
    parsed_events = _parse_order_events()
    if not parsed_events:
        return []
    entries = defaultdict(deque)
    reconstructed: list[dict] = []
    sequence = 0

    for ts, script, action, side, price, _order_id in parsed_events:
        if action == "ENTRY":
            entries[script].append({"side": side, "price": price, "ts": ts})
            continue

        if not entries[script]:
            continue

        entry = _pop_matching_entry(entries[script], side)
        if entry is None:
            continue
        lot = float(LOT_SIZES.get(script, 1))
        if entry["side"] == "BUY" and side == "SELL":
            realized = (price - entry["price"]) * lot
        elif entry["side"] == "SELL" and side == "BUY":
            realized = (entry["price"] - price) * lot
        else:
            realized = 0.0

        sequence += 1
        reconstructed.append(
            {
                "id": f"{script}-{int(ts.timestamp() * 1000)}-{sequence}",
                "symbol": script,
                "side": entry["side"],
                "quantity": lot,
                "entry_price": float(entry["price"]),
                "exit_price": float(price),
                "last_price": float(price),
                "unrealized_pnl": 0.0,
                "realized_pnl": round(realized, 2),
                "opened_at": entry["ts"].isoformat(),
                "closed_at": ts.isoformat(),
            }
        )

    # Newest first for UI table
    reconstructed.reverse()
    return reconstructed[:limit]


def _trade_date_text(trade: dict) -> str:
    closed_at = str(trade.get("closed_at") or "")
    if len(closed_at) >= 10:
        return closed_at[:10]
    opened_at = str(trade.get("opened_at") or "")
    if len(opened_at) >= 10:
        return opened_at[:10]
    return ""


def _closed_at_calendar_date(trade: dict) -> date | None:
    """Parse YYYY-MM-DD from closed_at for rolling-window stats."""
    text = str(trade.get("closed_at") or "").strip()
    if len(text) < 10:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _compute_symbol_performance(days: int = 14) -> dict:
    """
    Aggregate realized P&L by symbol over the last N calendar days (inclusive of today),
    using the same reconstructed closed trades as the dashboard tables.
    """
    safe_days = max(1, min(int(days), 366))
    today = datetime.now().date()
    cutoff = today - timedelta(days=safe_days - 1)

    effective = _effective_closed_trades(limit=5000)
    filtered: list[dict] = []
    for trade in effective:
        closed_day = _closed_at_calendar_date(trade)
        if closed_day is None or closed_day < cutoff:
            continue
        filtered.append(trade)

    by_symbol: dict[str, list[float]] = defaultdict(list)
    for trade in filtered:
        sym = str(trade.get("symbol") or "UNKNOWN").strip() or "UNKNOWN"
        pnl = float(trade.get("realized_pnl") or 0.0)
        by_symbol[sym].append(pnl)

    rows: list[dict] = []
    for symbol in sorted(by_symbol.keys()):
        pnls = by_symbol[symbol]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0.0)
        losses = sum(1 for p in pnls if p < 0.0)
        flat = n - wins - losses
        total = round(sum(pnls), 2)
        win_rate = round((wins / n) * 100.0, 1) if n else 0.0
        avg = round(total / n, 2) if n else 0.0
        rows.append(
            {
                "symbol": symbol,
                "trades": n,
                "wins": wins,
                "losses": losses,
                "breakeven": flat,
                "win_rate_pct": win_rate,
                "total_pnl": total,
                "avg_pnl": avg,
            }
        )

    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return {
        "period_days": safe_days,
        "cutoff_date": cutoff.isoformat(),
        "end_date": today.isoformat(),
        "trade_count": len(filtered),
        "rows": rows,
    }


def _normalize_iso_second(value: str | None) -> str:
    """
    Normalize timestamp to second precision for dedupe stability.
    Handles values like:
    - 2026-03-16T17:20:38.981092
    - 2026-03-16T17:20:38.980000
    - 2026-03-16 17:20:38,980
    """
    text = str(value or "").strip()
    if not text:
        return ""

    text = text.replace(",", ".")
    # Keep up to seconds only; timezone/frac differences are ignored for dedupe key
    if len(text) >= 19:
        return text[:19]
    return text


def _dedupe_closed_trades(trades: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for trade in trades:
        key = (
            trade.get("symbol"),
            trade.get("side"),
            round(float(trade.get("entry_price", 0) or 0), 4),
            round(float(trade.get("exit_price", 0) or 0), 4),
            round(float(trade.get("quantity", 0) or 0), 4),
            _normalize_iso_second(trade.get("opened_at")),
            _normalize_iso_second(trade.get("closed_at")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(trade)
    return deduped


def _closed_trade_open_identity_key(trade: dict) -> tuple:
    """Key for the same logical position irrespective of close-time drift."""
    return (
        trade.get("symbol"),
        trade.get("side"),
        round(float(trade.get("quantity", 0) or 0), 4),
        round(float(trade.get("entry_price", 0) or 0), 4),
        # Keep full opened_at precision to preserve separately-opened duplicates.
        str(trade.get("opened_at") or ""),
    )


def _trade_realized_value(trade: dict) -> float:
    try:
        return float(trade.get("realized_pnl", 0) or 0)
    except Exception:
        return 0.0


def _prefer_closed_trade(candidate: dict, existing: dict) -> bool:
    """
    Decide whether candidate should replace existing for same open identity.
    Preference:
    1) non-zero realized pnl
    2) later closed_at
    """
    cand_realized = abs(_trade_realized_value(candidate))
    exist_realized = abs(_trade_realized_value(existing))
    if cand_realized != exist_realized:
        return cand_realized > exist_realized
    return str(candidate.get("closed_at") or "") >= str(existing.get("closed_at") or "")


def _live_trade_identity_key(trade: dict) -> tuple:
    """
    Stable key for the same live position across minor ID/timestamp drift.
    """
    return (
        trade.get("symbol"),
        trade.get("side"),
        round(float(trade.get("quantity", 0) or 0), 4),
        round(float(trade.get("entry_price", 0) or 0), 4),
        _normalize_iso_second(trade.get("opened_at")),
    )


def _dedupe_live_trades(trades: list[dict]) -> list[dict]:
    deduped_by_key: dict[tuple, dict] = {}
    for trade in trades:
        key = _live_trade_identity_key(trade)
        previous = deduped_by_key.get(key)
        # Keep the most recent update by timestamp-ish fields.
        current_rank = (
            str(trade.get("opened_at") or ""),
            str(trade.get("id") or ""),
        )
        previous_rank = (
            str(previous.get("opened_at") or ""),
            str(previous.get("id") or ""),
        ) if previous else ("", "")
        if previous is None or current_rank >= previous_rank:
            deduped_by_key[key] = trade
    return list(deduped_by_key.values())


def _upsert_live_trade(payload: dict) -> None:
    """
    Upsert a live trade while removing semantic duplicates with different IDs.
    """
    target_key = _live_trade_identity_key(payload)
    stale_ids = []
    for trade_id, existing in live_trades.items():
        if trade_id == payload["id"]:
            continue
        if _live_trade_identity_key(existing) == target_key:
            stale_ids.append(trade_id)
    for trade_id in stale_ids:
        live_trades.pop(trade_id, None)
    live_trades[payload["id"]] = payload


def _effective_closed_trades(limit: int = 1000) -> list[dict]:
    """
    Source of truth for closed trades:
    - reconstructed trades from orders.log + archive
    - merged with in-memory closed trades for immediate UI freshness
    """
    reconstructed = _build_closed_trades_from_orders(limit=limit)
    by_open_identity: dict[tuple, dict] = {}

    # Start with log-reconstructed trades as baseline truth.
    for trade in reconstructed:
        by_open_identity[_closed_trade_open_identity_key(trade)] = trade

    # Overlay in-memory closed trades only when missing from log reconstruction.
    # This keeps orders.log/archive as canonical history and avoids showing
    # a later in-memory close timestamp for a position that was already
    # square-off/closed in archived logs.
    for trade in closed_trades:
        key = _closed_trade_open_identity_key(trade)
        existing = by_open_identity.get(key)
        if existing is None:
            by_open_identity[key] = trade

    merged = _dedupe_closed_trades(list(by_open_identity.values()))
    merged.sort(key=lambda t: t.get("closed_at") or "", reverse=True)
    return merged[:limit]


def _closed_trade_dates(trades: list[dict]) -> list[str]:
    dates = {d for d in (_trade_date_text(trade) for trade in trades) if d}
    return sorted(dates, reverse=True)


def _filter_closed_trades_by_date(trades: list[dict], date_text: str | None) -> list[dict]:
    if not date_text:
        return trades
    return [trade for trade in trades if _trade_date_text(trade) == date_text]


@app.get("/api/dashboard/initial")
async def dashboard_initial():
    today_text = datetime.now().date().isoformat()
    effective_closed = _effective_closed_trades(limit=2000)
    date_filtered_closed = _filter_closed_trades_by_date(effective_closed, today_text)
    weekly_points = _compute_weekly_pnl_from_orders(week_offset=0)
    return {
        "live_trades": _dedupe_live_trades(list(live_trades.values())),
        "closed_trades": date_filtered_closed,
        "closed_trade_dates": _closed_trade_dates(effective_closed),
        "closed_trade_selected_date": today_text,
        "weekly_pnl": weekly_points,
        "weekly_total": _weekly_total(weekly_points),
        "weekly_selected_offset": 0,
        "weekly_filter_options": _weekly_filter_options(count=12),
        "server_time": datetime.utcnow().isoformat(),
    }


@app.get("/api/dashboard/symbol-performance")
async def dashboard_symbol_performance(days: int = 14):
    """Per-symbol stats from reconstructed closed trades (orders.log + archive)."""
    try:
        safe_days = int(days)
    except (TypeError, ValueError):
        safe_days = 14
    return _compute_symbol_performance(safe_days)


@app.get("/api/dashboard/closed-trades")
async def dashboard_closed_trades(date: str | None = None):
    # date expected in YYYY-MM-DD format; invalid format returns empty set safely.
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"closed_trades": [], "closed_trade_dates": [], "selected_date": date}

    effective_closed = _effective_closed_trades(limit=2000)
    return {
        "closed_trades": _filter_closed_trades_by_date(effective_closed, date),
        "closed_trade_dates": _closed_trade_dates(effective_closed),
        "selected_date": date,
    }


@app.get("/api/dashboard/weekly-pnl")
async def dashboard_weekly_pnl(week_offset: int = 0):
    safe_offset = max(0, int(week_offset))
    weekly_points = _compute_weekly_pnl_from_orders(week_offset=safe_offset)
    return {
        "weekly_pnl": weekly_points,
        "weekly_total": _weekly_total(weekly_points),
        "weekly_selected_offset": safe_offset,
        "weekly_filter_options": _weekly_filter_options(count=12),
    }


@app.websocket("/ws/trades")
async def ws_trades(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        while True:
            # keep alive; client messages are optional
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in ws_clients:
            ws_clients.remove(websocket)
    except Exception:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


@app.post("/api/trade/open")
async def trade_open(trade: Trade):
    payload = trade.model_dump()
    _upsert_live_trade(payload)
    await _broadcast({"type": "trade_opened", "trade": payload})
    return {"ok": True}


@app.post("/api/trade/update")
async def trade_update(trade: Trade):
    payload = trade.model_dump()
    _upsert_live_trade(payload)
    await _broadcast({"type": "trade_updated", "trade": payload})
    return {"ok": True}


@app.post("/api/trades/update-batch")
async def trade_update_batch(trades: list[Trade]):
    if not trades:
        return {"ok": True, "updated": 0}

    updated_payloads = []
    for trade in trades:
        payload = trade.model_dump()
        _upsert_live_trade(payload)
        updated_payloads.append(payload)

    await _broadcast({"type": "trades_updated_batch", "trades": updated_payloads})
    return {"ok": True, "updated": len(updated_payloads)}


@app.post("/api/trade/close")
async def trade_close(trade: Trade):
    payload = trade.model_dump()
    if not payload.get("closed_at"):
        payload["closed_at"] = datetime.utcnow().isoformat()

    # Backfill realized P&L when caller sends close without finalized realized value.
    if payload.get("realized_pnl") is None:
        if payload.get("unrealized_pnl") is not None:
            payload["realized_pnl"] = round(float(payload.get("unrealized_pnl") or 0), 2)
        elif payload.get("entry_price") is not None and payload.get("exit_price") is not None:
            qty = float(payload.get("quantity") or 0)
            entry = float(payload.get("entry_price") or 0)
            exit_price = float(payload.get("exit_price") or 0)
            side = payload.get("side")
            if side == "BUY":
                payload["realized_pnl"] = round((exit_price - entry) * qty, 2)
            else:
                payload["realized_pnl"] = round((entry - exit_price) * qty, 2)
        else:
            payload["realized_pnl"] = 0.0
    payload["unrealized_pnl"] = 0.0

    live_trades.pop(payload["id"], None)
    # If the same position was tracked with alternate IDs, clear those too.
    close_key = _live_trade_identity_key(payload)
    for trade_id in list(live_trades.keys()):
        existing = live_trades.get(trade_id)
        if existing and _live_trade_identity_key(existing) == close_key:
            live_trades.pop(trade_id, None)
    closed_trades.insert(0, payload)
    _sort_closed_trades()
    await _broadcast({"type": "trade_closed", "trade": payload})
    await _broadcast({"type": "pnl_update", "weekly_pnl": _compute_weekly_pnl_from_orders(week_offset=0)})
    return {"ok": True}


@app.post("/api/weekly-pnl")
async def set_weekly_pnl(points: list[WeeklyPnlPoint]):
    # Kept for backward compatibility, but dashboard now uses orders.log as source of truth.
    _ = [point.model_dump() for point in points]
    computed = _compute_weekly_pnl_from_orders(week_offset=0)
    await _broadcast({"type": "pnl_update", "weekly_pnl": computed})
    return {"ok": True, "points": len(computed), "source": "orders.log"}


@app.get("/api/settings/upstox")
async def get_upstox_settings():
    data = read_credentials_file()
    admin_required = bool(os.environ.get("DASHBOARD_ADMIN_TOKEN", "").strip())
    return {
        "base_url": data["base_url"],
        "access_token_preview": mask_tail(data["access_token"]),
        "api_key_preview": mask_tail(data["api_key"]),
        "api_secret_preview": mask_tail(data["api_secret"]),
        "has_access_token": bool(data["access_token"]),
        "has_api_key": bool(data["api_key"]),
        "has_api_secret": bool(data["api_secret"]),
        "credentials_file": CREDENTIALS_FILE.name,
        "credentials_path": str(CREDENTIALS_FILE.resolve()),
        "admin_token_configured": admin_required,
    }


@app.post("/api/settings/upstox")
async def post_upstox_settings(request: Request, body: UpstoxSettingsBody):
    _require_dashboard_admin(request)
    current = read_credentials_file()
    updated = False
    if body.access_token.strip():
        current["access_token"] = normalize_access_token(body.access_token)
        updated = True
    if body.api_key.strip():
        current["api_key"] = body.api_key.strip()
        updated = True
    if body.api_secret.strip():
        current["api_secret"] = body.api_secret.strip()
        updated = True
    if body.base_url.strip():
        current["base_url"] = body.base_url.strip()
        updated = True
    persist_credentials(current)
    restart_result = None
    if updated:
        restart_result = await asyncio.to_thread(restart_trading_bot_after_credential_save)
    return {
        "ok": True,
        "saved": CREDENTIALS_FILE.name,
        "bot_restart": restart_result or {"restarted": False, "skipped": "no credential fields changed"},
    }
