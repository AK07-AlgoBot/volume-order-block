from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import List, Literal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


app = FastAPI(title="AK07 Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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


def _compute_weekly_pnl_from_orders(week_offset: int = 0) -> list[dict]:
    """Compute realized P&L for selected week's Monday-Friday from active+archived order logs."""
    weekdays = _week_monday_to_friday(week_offset)
    weekday_set = set(weekdays)
    pnl_by_date = {day: 0.0 for day in weekdays}

    order_files = _get_order_log_files()
    if not order_files:
        return [
            {"date": day.strftime("%Y-%m-%d"), "pnl": 0.0}
            for day in weekdays
        ]

    parsed_events = []
    for order_file in order_files:
        lines = order_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        for raw in lines:
            match = LINE_PATTERN.search(raw.strip())
            if not match:
                continue
            ts = _parse_ts(match.group("ts"))
            parsed_events.append(
                (
                    ts,
                    match.group("script").strip(),
                    match.group("action"),
                    match.group("side"),
                    float(match.group("price")),
                )
            )

    parsed_events.sort(key=lambda event: event[0])
    entries = defaultdict(deque)

    for ts, script, action, side, price in parsed_events:

        if action == "ENTRY":
            entries[script].append({"side": side, "price": price, "ts": ts})
            continue

        if not entries[script]:
            continue

        entry = entries[script].popleft()
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
    order_files = _get_order_log_files()
    if not order_files:
        return []

    parsed_events = []
    for order_file in order_files:
        lines = order_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        for raw in lines:
            match = LINE_PATTERN.search(raw.strip())
            if not match:
                continue
            parsed_events.append(
                (
                    _parse_ts(match.group("ts")),
                    match.group("script").strip(),
                    match.group("action"),
                    match.group("side"),
                    float(match.group("price")),
                )
            )

    parsed_events.sort(key=lambda event: event[0])
    entries = defaultdict(deque)
    reconstructed: list[dict] = []
    sequence = 0

    for ts, script, action, side, price in parsed_events:
        if action == "ENTRY":
            entries[script].append({"side": side, "price": price, "ts": ts})
            continue

        if not entries[script]:
            continue

        entry = entries[script].popleft()
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

    # Overlay in-memory closed trades only when they are better/missing.
    for trade in closed_trades:
        key = _closed_trade_open_identity_key(trade)
        existing = by_open_identity.get(key)
        if existing is None or _prefer_closed_trade(trade, existing):
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
