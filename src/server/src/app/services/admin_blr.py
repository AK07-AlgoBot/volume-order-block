"""Admin-managed global BLR levels."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import cache_manager
from app.services.breakout_engine import INDEX_CONFIGS, IST, day_review_from_first_close
from app.services.engine_intraday import blr_day_review_allows_direction


def get_blr_state(index_code: str) -> dict[str, Any]:
    code = index_code.strip().upper()
    state = cache_manager.get_json(
        cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=code)
    )
    return state if isinstance(state, dict) else {}


def update_blr_levels(
    *,
    index_code: str,
    green: float,
    mid: float,
    red: float,
    updated_by: str,
) -> dict[str, Any]:
    """Persist a manual BLR override and publish it to every dashboard."""
    code = index_code.strip().upper()
    cfg = INDEX_CONFIGS.get(code)
    if cfg is None:
        raise ValueError(f"Unknown index: {index_code}")
    if not green > mid > red:
        raise ValueError("BLR values must satisfy Green > Mid > Red.")

    now = datetime.now(IST)
    day = now.date().isoformat()
    updated_at = now.isoformat()
    state = get_blr_state(code)
    first_close_raw = state.get("first_candle_close")
    first_close = float(first_close_raw) if first_close_raw is not None else None
    if first_close is None:
        day_review = str(state.get("day_review") or "PENDING")
    else:
        day_review = day_review_from_first_close(first_close, mid)

    band_half = round((green - red) / 2.0, 4)
    band_half_pct = round(band_half / mid * 100.0, 6)
    common = {
        "mid": float(mid),
        "green": float(green),
        "red": float(red),
        "gap_regime": str(state.get("gap_regime") or "MANUAL"),
        "band_half": band_half,
        "band_half_pct": band_half_pct,
        "session_open": float(mid),
        "broker_session_open": float(mid),
        "session_open_tv_offset": 0.0,
        "session_open_source": "manual_admin",
        "prev_close": state.get("prev_close"),
        "day_review": day_review,
        "first_candle_close": first_close,
        "admin_updated_at": updated_at,
        "admin_updated_by": updated_by,
    }
    cache_manager.set_json(
        cache_manager.BREAKOUT_FROZEN_KEY_TEMPLATE.format(day=day, index=code),
        common,
        ttl_seconds=86_400 * 2,
    )
    cache_manager.set_json(
        cache_manager.BREAKOUT_OPEN_TICK_KEY_TEMPLATE.format(day=day, index=code),
        {
            "price": float(mid),
            "captured_at": updated_at,
            "source": "manual_admin",
        },
        ttl_seconds=86_400 * 2,
    )

    state.update(
        {
            "index": code,
            "display": cfg.display,
            **common,
            "levels_ready": True,
            "allowed_long": blr_day_review_allows_direction(day_review, "LONG"),
            "allowed_short": blr_day_review_allows_direction(day_review, "SHORT"),
            "setup_label": (
                f"BLR updated by admin — G {green:.2f} / M {mid:.2f} / "
                f"R {red:.2f} (review {day_review})"
            ),
            "updated_at": updated_at,
        }
    )
    cache_manager.set_json(
        cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=code),
        state,
        ttl_seconds=86_400,
    )
    return state
