"""Daily Upstox P&L profit target — blocks new bot entries when hit.

Uses broker short-term position P&L (not dashboard spot estimates).
Does NOT square off open positions (manual / other traders may stay open).
Telegram signals and follow-on alerts continue; only Upstox BUY orders stop.

Rules (INR, from env defaults):
  - Expiry days (any index Tue/Thu/monthly BN expiry): stop at TARGET_EXPIRY (3000).
  - Normal days: stop at TARGET_NORMAL (5000) across multiple trades.
  - Normal days, first entry only: if Upstox day P&L >= TARGET_FIRST (3000) before a
    second entry, stop immediately at 3000.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time as dtime
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.services import cache_manager, telegram_notifier
from app.services.expiry_calendar import EXPIRY_RULES, is_index_expiry

logger = logging.getLogger("ak07.daily_profit_guard")

IST: Final = ZoneInfo("Asia/Kolkata")

TARGET_EXPIRY_INR: Final[float] = float(os.environ.get("AK07_DAILY_TARGET_EXPIRY_INR", "3000"))
TARGET_NORMAL_INR: Final[float] = float(os.environ.get("AK07_DAILY_TARGET_NORMAL_INR", "5000"))
TARGET_FIRST_TRADE_INR: Final[float] = float(os.environ.get("AK07_DAILY_TARGET_FIRST_TRADE_INR", "3000"))

SESSION_START: Final[dtime] = dtime(9, 0)
SESSION_END: Final[dtime] = dtime(15, 45)


def is_expiry_trading_day(day: date | None = None) -> bool:
    """True when any tracked index expires today (NIFTY Tue, SENSEX Thu, BN last Thu)."""
    day = day or datetime.now(IST).date()
    return any(is_index_expiry(code, day) for code in EXPIRY_RULES)


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "day": _today(),
        "entries_today": 0,
        "engaged": False,
        "target_inr": TARGET_NORMAL_INR,
        "upstox_pnl_inr": 0.0,
        "upstox_realised_inr": 0.0,
        "upstox_unrealised_inr": 0.0,
        "engaged_at": "",
        "engaged_reason": "",
        "expiry_day": False,
    }


def load_state() -> dict[str, Any]:
    raw = cache_manager.get_json(cache_manager.DAILY_PROFIT_TARGET_KEY)
    if not isinstance(raw, dict):
        return _default_state()
    if str(raw.get("day") or "") != _today():
        return _default_state()
    return {**_default_state(), **raw}


def save_state(state: dict[str, Any]) -> None:
    cache_manager.set_json(cache_manager.DAILY_PROFIT_TARGET_KEY, state, ttl_seconds=86_400)


def profit_target_engaged() -> bool:
    state = load_state()
    return bool(state.get("engaged"))


def record_broker_entry() -> None:
    """Call after a successful Upstox BUY (intraday entry)."""
    state = load_state()
    state["entries_today"] = int(state.get("entries_today") or 0) + 1
    save_state(state)
    logger.info("Daily profit guard: broker entry #%d today", state["entries_today"])


def effective_target_inr(entries_today: int, expiry_day: bool) -> float:
    if expiry_day:
        return TARGET_EXPIRY_INR
    return TARGET_NORMAL_INR


def should_engage_target(
    total_pnl_inr: float,
    entries_today: int,
    expiry_day: bool,
) -> tuple[bool, float, str]:
    """Return (engage, target_inr, human reason)."""
    if expiry_day:
        target = TARGET_EXPIRY_INR
        if total_pnl_inr >= target:
            return True, target, f"Expiry day — Upstox P&L Rs.{total_pnl_inr:,.0f} >= Rs.{target:,.0f}"
        return False, target, ""

    if entries_today <= 1 and total_pnl_inr >= TARGET_FIRST_TRADE_INR:
        return (
            True,
            TARGET_FIRST_TRADE_INR,
            f"First trade day P&L Rs.{total_pnl_inr:,.0f} >= Rs.{TARGET_FIRST_TRADE_INR:,.0f} — locking gains",
        )

    target = TARGET_NORMAL_INR
    if total_pnl_inr >= target:
        return True, target, f"Normal day target Rs.{target:,.0f} reached (Upstox P&L Rs.{total_pnl_inr:,.0f})"
    return False, target, ""


def publish_upstox_pnl_snapshot(pnl: dict[str, float]) -> None:
    cache_manager.set_json(
        cache_manager.UPSTOX_DAILY_PNL_KEY,
        {
            "day": _today(),
            "total_pnl_inr": round(pnl.get("total_pnl", 0.0), 2),
            "realised_inr": round(pnl.get("realised", 0.0), 2),
            "unrealised_inr": round(pnl.get("unrealised", 0.0), 2),
            "open_positions": int(pnl.get("open_positions", 0)),
            "updated_at": datetime.now(IST).isoformat(),
        },
        ttl_seconds=120,
    )


def engage_daily_profit_target(
    *,
    total_pnl_inr: float,
    target_inr: float,
    reason: str,
) -> None:
    """Mark target hit and block new bot BUY orders for the session."""
    state = load_state()
    if state.get("engaged"):
        return

    now = datetime.now(IST)
    state.update(
        {
            "engaged": True,
            "target_inr": target_inr,
            "upstox_pnl_inr": round(total_pnl_inr, 2),
            "engaged_at": now.isoformat(),
            "engaged_reason": reason,
            "expiry_day": is_expiry_trading_day(now.date()),
        }
    )
    save_state(state)

    detail = (
        f"Upstox day P&L: Rs.{total_pnl_inr:,.0f}\n"
        f"AK07 bot target: Rs.{target_inr:,.0f}\n"
        f"{reason}\n\n"
        "Bot new entries blocked on Upstox for today.\n"
        "Open positions (yours or others) are NOT touched.\n"
        "Telegram will keep sending new signals and follow-on trade instructions.\n"
        "Manual trading on Upstox can continue with separate targets."
    )

    logger.warning("DAILY PROFIT TARGET ENGAGED: %s", reason)
    telegram_notifier.notify_system_event("AK07 DAILY TARGET HIT — BOT ENTRIES OFF", detail)
