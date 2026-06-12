"""Shared intraday session helpers for AK07 strategy engines."""

from __future__ import annotations

import os
from datetime import datetime, time as dtime
from typing import Final
from zoneinfo import ZoneInfo

from app.services import cache_manager

IST: Final = ZoneInfo("Asia/Kolkata")


def parse_ist_time(env_key: str, default_hour: int, default_minute: int) -> dtime:
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        parts = raw.replace(".", ":").split(":")
        if len(parts) >= 2:
            try:
                return dtime(int(parts[0]), int(parts[1]))
            except ValueError:
                pass
    return dtime(default_hour, default_minute)


def kill_switch_engaged() -> bool:
    flag = cache_manager.get_json(cache_manager.KILL_SWITCH_KEY)
    return bool(flag and flag.get("engaged"))


def rr_book_targets(entry: float, sl: float, direction: str) -> tuple[float, float, float]:
    risk = max(abs(entry - sl), 0.05)
    if direction == "LONG":
        return entry + risk, entry + 2.0 * risk, risk
    return entry - risk, entry - 2.0 * risk, risk


def session_vwap(candles: list[dict[str, float]]) -> float | None:
    num = den = 0.0
    for c in candles:
        vol = float(c.get("volume") or 0)
        if vol <= 0:
            continue
        typical = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        num += typical * vol
        den += vol
    return num / den if den > 0 else None
