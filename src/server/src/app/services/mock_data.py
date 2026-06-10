"""Mock market data seeder for cockpit verification (no broker, no live feed).

Used by the dashboard when AK07_MOCK=1: every rerun nudges the seeded values
with a small random walk so the cockpit looks and feels alive - spot drifts,
component blocks flip color, live P&L moves - without ever touching the
Upstox API. Works against real Redis or the in-process fakeredis that
cache_manager activates in mock mode.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.services import cache_manager
from app.services.upstox_engine import INDEX_CONFIGS, component_bias

IST: Final = ZoneInfo("Asia/Kolkata")

_BASELINES: Final[dict[str, dict[str, float]]] = {
    "NIFTY": {"spot": 24123.50, "step": 50},
    "BANKNIFTY": {"spot": 51840.00, "step": 100},
    "SENSEX": {"spot": 79215.00, "step": 100},
}


def _walk(value: float, max_pct: float = 0.0006) -> float:
    return value * (1 + random.uniform(-max_pct, max_pct))


def seed() -> None:
    """Write one full cockpit frame (idempotent; call on every dashboard rerun)."""
    now = datetime.now(IST)

    if cache_manager.get_json(cache_manager.ENGINE_HEARTBEAT_KEY) is None:
        cache_manager.set_system_bias("NEUTRAL")

    kill_flag = cache_manager.get_json(cache_manager.KILL_SWITCH_KEY)
    kill_engaged = bool(kill_flag and isinstance(kill_flag, dict) and kill_flag.get("engaged"))

    positions: dict[str, Any] = {}
    for code, cfg in INDEX_CONFIGS.items():
        key = cache_manager.INDEX_STATE_KEY_TEMPLATE.format(index=code)
        previous = cache_manager.get_json(key) or {}

        spot = _walk(float(previous.get("spot") or _BASELINES[code]["spot"]))
        step = int(_BASELINES[code]["step"])
        call_wall = int(round((spot + 3.5 * step) / step) * step)
        put_floor = int(round((spot - 3.0 * step) / step) * step)

        prev_components: dict[str, Any] = previous.get("components") or {}
        components = {
            symbol: round(
                max(-3.5, min(3.5, float(prev_components.get(symbol) or random.uniform(-1.2, 1.2))
                              + random.uniform(-0.12, 0.12))),
                2,
            )
            for symbol in cfg.heavyweights
        }

        # Keep one demo position alive on NIFTY so the cockpit shows the 2-lot flow.
        position = None
        if code == "NIFTY" and not kill_engaged:
            prev_pos = previous.get("position") or {}
            entry = float(prev_pos.get("entry_price") or spot - 35)
            partial = bool(prev_pos.get("partial_booked", (spot - entry) >= 60))
            position = {
                "index_code": code,
                "direction": "LONG",
                "entry_price": round(entry, 2),
                "target_price": round(entry + 120, 2),
                "sl_price": round(entry - 60, 2),
                "lot_size": cfg.lot_size,
                "lots_remaining": 1 if partial else 2,
                "partial_booked": partial,
                "quantity": cfg.lot_size * (1 if partial else 2),
                "instrument_key": "",
                "option_strike": int(round((entry - 50) / step) * step),
                "option_type": "CE",
                "opened_at": prev_pos.get("opened_at") or now.isoformat(),
            }
            positions[code] = position

        cache_manager.set_json(
            key,
            {
                "index": code,
                "display": cfg.display,
                "spot": round(spot, 2),
                "call_wall": call_wall,
                "put_floor": put_floor,
                "component_bias": component_bias(components),
                "components": components,
                "trades_today": 1,
                "max_trades": 2,
                "position": position,
                "entries_blocked": kill_engaged,
                "paper_trading": True,
                "updated_at": now.isoformat(),
            },
            ttl_seconds=120,
        )

    cache_manager.set_json(cache_manager.POSITIONS_KEY, positions)
    cache_manager.set_json(
        cache_manager.ENGINE_HEARTBEAT_KEY,
        {"at": now.isoformat(), "paper_trading": True, "mock": True},
        ttl_seconds=60,
    )

    nifty = cache_manager.get_json(cache_manager.INDEX_STATE_KEY_TEMPLATE.format(index="NIFTY")) or {}
    if nifty:
        cache_manager.set_market_snapshot(
            {
                "spot_price": float(nifty["spot"]),
                "volume": random.randint(140_000, 220_000),
                "highest_call_oi_strike": int(nifty["call_wall"]),
                "highest_put_oi_strike": int(nifty["put_floor"]),
                "timestamp": now.isoformat(),
            }
        )
