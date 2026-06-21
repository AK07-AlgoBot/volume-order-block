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


# Fixed spot targets (Strategy 3 BLR book, Strategy 5 Greeks)
FIXED_TP1_PTS: Final[dict[str, float]] = {
    "NIFTY": float(os.environ.get("AK07_FIXED_TP1_PTS_NIFTY", "30")),
    "BANKNIFTY": float(os.environ.get("AK07_FIXED_TP1_PTS_BANKNIFTY", "30")),
    "SENSEX": float(os.environ.get("AK07_FIXED_TP1_PTS_SENSEX", "100")),
}
FIXED_SL_PTS: Final[dict[str, float]] = {
    "NIFTY": float(os.environ.get("AK07_FIXED_SL_PTS_NIFTY", "30")),
    "BANKNIFTY": float(os.environ.get("AK07_FIXED_SL_PTS_BANKNIFTY", "30")),
    "SENSEX": float(os.environ.get("AK07_FIXED_SL_PTS_SENSEX", "60")),
}


def fixed_book_targets(index_code: str, entry: float, direction: str) -> tuple[float, float, float]:
    """TP1/TP2 from fixed index points (book @ TP1)."""
    tp1_pts = FIXED_TP1_PTS.get(index_code, 30.0)
    tp2_pts = tp1_pts * 2.0
    if direction == "LONG":
        return entry + tp1_pts, entry + tp2_pts, tp1_pts
    return entry - tp1_pts, entry - tp2_pts, tp1_pts


def fixed_sl_price(index_code: str, entry: float, direction: str) -> float:
    sl_pts = FIXED_SL_PTS.get(index_code, 30.0)
    if direction == "LONG":
        return entry - sl_pts
    return entry + sl_pts


def blr_day_review_allows_direction(day_review: str, direction: str) -> bool:
    """9:20 first 5m close vs Mid — master bias for S3 entries and S2/S4/S5."""
    if day_review in ("", "PENDING"):
        return False
    if day_review == "NEUTRAL":
        return True
    if direction == "LONG":
        return day_review == "LONG"
    if direction == "SHORT":
        return day_review == "SHORT"
    return False


def direction_allowed_by_blr_day(index_code: str, direction: str) -> tuple[bool, str]:
    """Return (allowed, note) using published Strategy 3 day review."""
    key = cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code)
    bo = cache_manager.get_json(key) or {}
    if not bo.get("levels_ready"):
        return False, "S3 BLR levels not ready"
    review = str(bo.get("day_review") or "PENDING")
    if review == "PENDING":
        return False, "S3 day review pending (await 9:20 5m close)"
    if blr_day_review_allows_direction(review, direction):
        return True, f"Review {review}"
    return False, f"S3 Review {review} — skip {direction}"


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
