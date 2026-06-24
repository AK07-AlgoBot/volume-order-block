"""Index expiry calendar rules for gamma / hero-zero observer.

NIFTY     — weekly Tuesday
BANKNIFTY — monthly last Thursday
SENSEX    — weekly Thursday
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final

EXPIRY_RULES: Final[dict[str, str]] = {
    "NIFTY": "weekly_tuesday",
    "BANKNIFTY": "monthly_last_thursday",
    "SENSEX": "weekly_thursday",
}


def _last_weekday_of_month(d: date, weekday: int) -> date:
    """Last occurrence of weekday (0=Mon … 6=Sun) in d's month."""
    if d.month == 12:
        nxt = date(d.year + 1, 1, 1)
    else:
        nxt = date(d.year, d.month + 1, 1)
    cur = nxt - timedelta(days=1)
    while cur.weekday() != weekday:
        cur -= timedelta(days=1)
    return cur


def is_index_expiry(index_code: str, day: date) -> bool:
    code = index_code.upper()
    rule = EXPIRY_RULES.get(code)
    if rule == "weekly_tuesday":
        return day.weekday() == 1
    if rule == "weekly_thursday":
        return day.weekday() == 3
    if rule == "monthly_last_thursday":
        return day == _last_weekday_of_month(day, 3)
    return False


def expiry_label(index_code: str) -> str:
    rule = EXPIRY_RULES.get(index_code.upper(), "")
    labels = {
        "weekly_tuesday": "Weekly · Tuesday",
        "weekly_thursday": "Weekly · Thursday",
        "monthly_last_thursday": "Monthly · last Thursday",
    }
    return labels.get(rule, rule or "unknown")


def iter_expiry_days(index_code: str, start: date, end: date) -> list[date]:
    out: list[date] = []
    cur = start
    while cur <= end:
        if is_index_expiry(index_code, cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out
