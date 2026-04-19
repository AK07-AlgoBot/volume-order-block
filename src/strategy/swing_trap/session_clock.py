from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _t(d: date, hms: tuple[int, int, int]) -> datetime:
    h, m, s = hms
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST)


def session_bounds(trading_day: date) -> tuple[datetime, datetime]:
    """NSE cash-style session window for a calendar day."""
    return _t(trading_day, (9, 15, 0)), _t(trading_day, (15, 30, 0))


def last_entry_allowed(trading_day: date, hms: tuple[int, int, int]) -> datetime:
    return _t(trading_day, hms)


def force_exit_deadline(trading_day: date, hms: tuple[int, int, int]) -> datetime:
    return _t(trading_day, hms)


def is_within_entry_window(ts: datetime, no_new_after: datetime) -> bool:
    t = ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)
    return t <= no_new_after


def is_at_or_after_force_exit(ts: datetime, deadline: datetime) -> bool:
    t = ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)
    return t >= deadline


def trading_day_for_ts(ts: datetime) -> date:
    t = ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)
    return t.date()
