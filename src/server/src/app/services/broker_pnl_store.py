"""Per-broker day P&L snapshots for dashboard (Upstox / Groww)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import cache_manager

IST = ZoneInfo("Asia/Kolkata")


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def publish_groww_pnl_snapshot(username: str, pnl: dict[str, float]) -> None:
    cache_manager.set_json(
        cache_manager.GROWW_DAILY_PNL_KEY_TEMPLATE.format(username=username),
        {
            "day": _today(),
            "username": username,
            "broker": "groww",
            "total_pnl_inr": round(float(pnl.get("total_pnl") or 0.0), 2),
            "realised_inr": round(float(pnl.get("realised") or 0.0), 2),
            "unrealised_inr": round(float(pnl.get("unrealised") or 0.0), 2),
            "open_positions": int(pnl.get("open_positions") or 0),
            "updated_at": datetime.now(IST).isoformat(),
        },
        ttl_seconds=300,
    )


def get_groww_pnl_snapshot(username: str) -> dict[str, Any]:
    raw = cache_manager.get_json(cache_manager.GROWW_DAILY_PNL_KEY_TEMPLATE.format(username=username))
    return raw if isinstance(raw, dict) else {}


def get_upstox_pnl_snapshot() -> dict[str, Any]:
    raw = cache_manager.get_json(cache_manager.UPSTOX_DAILY_PNL_KEY)
    return raw if isinstance(raw, dict) else {}


def get_user_broker_pnl(username: str, broker: str) -> dict[str, Any]:
    """Day P&L snapshot for dashboard — keyed by logged-in user's broker."""
    b = (broker or "upstox").strip().lower()
    if b == "groww":
        return get_groww_pnl_snapshot(username)
    return get_upstox_pnl_snapshot()


def broker_pnl_label(broker: str) -> str:
    return {"groww": "Groww P&L", "upstox": "Upstox P&L", "kite": "Kite P&L"}.get(
        (broker or "upstox").strip().lower(),
        "Broker P&L",
    )


def format_pnl_inr(value: float | None) -> str:
    if value is None:
        return "—"
    return f"₹{float(value):+,.0f}"


def refresh_groww_pnl_if_stale(username: str, *, max_age_sec: int = 15) -> dict[str, Any]:
    """Fetch Groww FNO day P&L when Redis snapshot is missing or old."""
    snap = get_groww_pnl_snapshot(username)
    updated = str(snap.get("updated_at") or "")
    if snap.get("day") == _today() and updated:
        try:
            ts = datetime.fromisoformat(updated)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            age = (datetime.now(IST) - ts.astimezone(IST)).total_seconds()
            if age <= max_age_sec:
                return snap
        except ValueError:
            pass
    from app.services.groww_engine import GrowwClient

    pnl = GrowwClient(username).get_fno_day_pnl()
    if pnl is not None:
        publish_groww_pnl_snapshot(username, pnl)
        return get_groww_pnl_snapshot(username)
    return snap
