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


def _current_week_monday_to_friday() -> list[datetime.date]:
    """Return current week's Monday-Friday dates in order."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
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


def _compute_weekly_pnl_from_orders(days: int = 5) -> list[dict]:
    """Compute weekly realized P&L for Monday-Friday from active+archived order logs."""
    if days <= 0:
        return []

    weekdays = _current_week_monday_to_friday()
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


@app.get("/api/dashboard/initial")
async def dashboard_initial():
    return {
        "live_trades": list(live_trades.values()),
        "closed_trades": closed_trades,
        "weekly_pnl": _compute_weekly_pnl_from_orders(days=5),
        "server_time": datetime.utcnow().isoformat(),
    }


@app.get("/api/dashboard/weekly-pnl")
async def dashboard_weekly_pnl():
    return {"weekly_pnl": _compute_weekly_pnl_from_orders(days=5)}


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
    live_trades[payload["id"]] = payload
    await _broadcast({"type": "trade_opened", "trade": payload})
    return {"ok": True}


@app.post("/api/trade/update")
async def trade_update(trade: Trade):
    payload = trade.model_dump()
    live_trades[payload["id"]] = payload
    await _broadcast({"type": "trade_updated", "trade": payload})
    return {"ok": True}


@app.post("/api/trades/update-batch")
async def trade_update_batch(trades: list[Trade]):
    if not trades:
        return {"ok": True, "updated": 0}

    updated_payloads = []
    for trade in trades:
        payload = trade.model_dump()
        live_trades[payload["id"]] = payload
        updated_payloads.append(payload)

    await _broadcast({"type": "trades_updated_batch", "trades": updated_payloads})
    return {"ok": True, "updated": len(updated_payloads)}


@app.post("/api/trade/close")
async def trade_close(trade: Trade):
    payload = trade.model_dump()
    live_trades.pop(payload["id"], None)
    closed_trades.insert(0, payload)
    _sort_closed_trades()
    await _broadcast({"type": "trade_closed", "trade": payload})
    await _broadcast({"type": "pnl_update", "weekly_pnl": _compute_weekly_pnl_from_orders(days=5)})
    return {"ok": True}


@app.post("/api/weekly-pnl")
async def set_weekly_pnl(points: list[WeeklyPnlPoint]):
    # Kept for backward compatibility, but dashboard now uses orders.log as source of truth.
    _ = [point.model_dump() for point in points]
    computed = _compute_weekly_pnl_from_orders(days=5)
    await _broadcast({"type": "pnl_update", "weekly_pnl": computed})
    return {"ok": True, "points": len(computed), "source": "orders.log"}
