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
from app.services.smc_crt_engine import SMC_CRT_INSTRUMENTS
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
                "monitoring_active": False,
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

    _seed_smc_crt(now, kill_engaged)
    _seed_breakout(now, kill_engaged)
    _seed_performance_trades(now)


def _seed_performance_trades(now: datetime) -> None:
    """Sample closed trades for the performance review page (mock mode)."""
    from app.services import performance_store  # noqa: PLC0415

    day = now.date().isoformat()
    key = performance_store.COMPLETED_TRADES_KEY_TEMPLATE.format(day=day)
    if cache_manager.get_json(key):
        return

    samples = [
        {
            "strategy": performance_store.STRATEGY_AK07_OI,
            "strategy_id": "ak07_oi",
            "symbol": "NIFTY",
            "direction": "LONG",
            "entry_price": 23100.0,
            "exit_price": 23165.0,
            "pnl_points": 65.0,
            "result": "WIN",
            "exit_reason": "PARTIAL_BOOK — HALF_TARGET_+60",
            "entry_at": now.isoformat(),
            "exit_at": now.isoformat(),
            "paper_trading": True,
        },
        {
            "strategy": performance_store.STRATEGY_AK07_OI,
            "strategy_id": "ak07_oi",
            "symbol": "NIFTY",
            "direction": "LONG",
            "entry_price": 23100.0,
            "exit_price": 23040.0,
            "pnl_points": -60.0,
            "result": "LOSS",
            "exit_reason": "STOP_LOSS",
            "entry_at": now.isoformat(),
            "exit_at": now.isoformat(),
            "paper_trading": True,
        },
        {
            "strategy": performance_store.STRATEGY_SMC_CRT,
            "strategy_id": "smc_crt",
            "symbol": "NIFTY",
            "direction": "LONG",
            "entry_price": 23100.0,
            "exit_price": 23135.0,
            "pnl_points": 35.0,
            "result": "WIN",
            "exit_reason": "TP1 CRM hit",
            "entry_at": now.isoformat(),
            "exit_at": now.isoformat(),
            "paper_trading": False,
        },
        {
            "strategy": performance_store.STRATEGY_SMC_CRT,
            "strategy_id": "smc_crt",
            "symbol": "NIFTY",
            "direction": "SHORT",
            "entry_price": 23150.0,
            "exit_price": 23190.0,
            "pnl_points": -40.0,
            "result": "LOSS",
            "exit_reason": "SL hit",
            "entry_at": now.isoformat(),
            "exit_at": now.isoformat(),
            "paper_trading": False,
        },
        {
            "strategy": performance_store.STRATEGY_BREAKOUT,
            "strategy_id": "breakout",
            "symbol": "BANKNIFTY",
            "direction": "LONG",
            "entry_price": 51200.0,
            "exit_price": 51255.0,
            "pnl_points": 55.0,
            "result": "WIN",
            "exit_reason": "TP1",
            "entry_at": now.isoformat(),
            "exit_at": now.isoformat(),
            "paper_trading": True,
        },
        {
            "strategy": performance_store.STRATEGY_BREAKOUT,
            "strategy_id": "breakout",
            "symbol": "SENSEX",
            "direction": "SHORT",
            "entry_price": 76400.0,
            "exit_price": 76435.0,
            "pnl_points": -35.0,
            "result": "LOSS",
            "exit_reason": "SL",
            "entry_at": now.isoformat(),
            "exit_at": now.isoformat(),
            "paper_trading": True,
        },
    ]
    cache_manager.set_json(key, samples, ttl_seconds=performance_store.TRADE_TTL_SECONDS)


def _seed_breakout(now: datetime, kill_engaged: bool) -> None:
    """Strategy Type 3 mock frames for Nifty / BankNifty / Sensex."""
    from app.services.breakout_engine import compute_blr_levels  # noqa: PLC0415

    for code, cfg in INDEX_CONFIGS.items():
        key = cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=code)
        previous = cache_manager.get_json(key) or {}
        spot = _walk(float(previous.get("spot") or _BASELINES[code]["spot"]))
        prev = {
            "open": spot - 120,
            "high": spot + 80,
            "low": spot - 180,
            "close": spot - 40,
        }
        levels = compute_blr_levels(prev["open"], prev["high"], prev["low"], prev["close"], spot, code)
        day_review = "LONG" if spot > levels.mid else "SHORT"

        position = None
        if code == "BANKNIFTY" and not kill_engaged:
            entry = levels.green - 15
            sl, tp1 = entry - 45, entry + 45
            position = {
                "direction": "LONG",
                "entry_price": round(entry, 2),
                "sl_price": round(sl, 2),
                "tp1_price": round(tp1, 2),
                "option_strike": int(round((entry - 50) / _BASELINES[code]["step"]) * _BASELINES[code]["step"]),
                "option_type": "CE",
                "entry_reason": "green breakout (review side)",
                "opened_at": previous.get("position", {}).get("opened_at") or now.isoformat(),
            }

        cache_manager.set_json(
            key,
            {
                "index": code,
                "display": cfg.display,
                "strategy": "Breakout",
                "spot": round(spot, 2),
                "mid": round(levels.mid, 2),
                "green": round(levels.green, 2),
                "red": round(levels.red, 2),
                "gap_regime": levels.gap_regime,
                "allowed_long": day_review in ("LONG", "NEUTRAL"),
                "allowed_short": day_review in ("SHORT", "NEUTRAL"),
                "levels_ready": True,
                "day_review": day_review,
                "first_candle_close": round(levels.mid + 12, 2),
                "setup_label": f"Review {day_review} side (mock BLR locked)",
                "trades_today": 1 if position else 0,
                "max_trades": 2,
                "entries_blocked": kill_engaged,
                "paper_trading": True,
                "position": position,
                "signals": previous.get("signals") or ["Mock breakout levels seeded"],
                "updated_at": now.isoformat(),
            },
            ttl_seconds=120,
        )

    cache_manager.set_json(
        cache_manager.BREAKOUT_HEARTBEAT_KEY,
        {
            "at": now.isoformat(),
            "paper_trading": True,
            "mock": True,
            "session_end_ist": "15:30",
            "indices": list(INDEX_CONFIGS.keys()),
        },
        ttl_seconds=60,
    )


def _seed_smc_crt(now: datetime, kill_engaged: bool) -> None:
    """Strategy Type 2 mock frames for each enabled SMC+CRT instrument."""
    if not SMC_CRT_INSTRUMENTS:
        return

    instrument_codes: list[str] = []
    for code, cfg in SMC_CRT_INSTRUMENTS.items():
        instrument_codes.append(code)
        key = cache_manager.SMC_CRT_STATE_KEY_TEMPLATE.format(symbol=code)
        previous = cache_manager.get_json(key) or {}
        spot = _walk(float(previous.get("spot") or cfg.baseline_spot), max_pct=0.0012)
        width = spot * 0.004
        crh = float(previous.get("crh") or spot + width / 2)
        crm = float(previous.get("crm") or spot)
        crl = float(previous.get("crl") or spot - width / 2)

        position = None
        if code == "BANKNIFTY" and not kill_engaged:
            prev_pos = previous.get("position") or {}
            entry = float(prev_pos.get("entry_price") or crl + (crm - crl) * 0.3)
            bn_step = int(_BASELINES.get("BANKNIFTY", {}).get("step") or 100)
            position = {
                "direction": "LONG",
                "entry_price": round(entry, 2),
                "sl_price": round(entry - width * 0.35, 2),
                "tp1_price": round(crm, 2),
                "tp2_price": round(crh, 2),
                "option_strike": int(round((entry - 50) / bn_step) * bn_step),
                "option_type": "CE",
                "quantity": INDEX_CONFIGS["BANKNIFTY"].lot_size,
                "opened_at": prev_pos.get("opened_at") or now.isoformat(),
            }

        cache_manager.set_json(
            key,
            {
                "symbol": code,
                "display": cfg.display,
                "strategy": "SMC+CRT",
                "spot": round(spot, 2),
                "crh": round(crh, 2),
                "crm": round(crm, 2),
                "crl": round(crl, 2),
                "crt_ready": True,
                "setup_label": "CRT locked — watching 5m FVG (mock)",
                "swept_low": True,
                "swept_high": False,
                "paper_trading": False,
                "entries_blocked": kill_engaged,
                "trades_today": 1 if position else 0,
                "max_trades": 2,
                "lots_per_trade": 1,
                "session_end_ist": "15:30",
                "fvg": {
                    "direction": "LONG",
                    "low": round(crl + width * 0.1, 2),
                    "high": round(crl + width * 0.25, 2),
                    "candle_ts": now.isoformat(),
                },
                "position": position,
                "signals": previous.get("signals") or ["Mock CRT range seeded"],
                "updated_at": now.isoformat(),
            },
            ttl_seconds=120,
        )

    cache_manager.set_json(
        cache_manager.SMC_CRT_HEARTBEAT_KEY,
        {
            "at": now.isoformat(),
            "paper_trading": False,
            "mock": True,
            "session_end_ist": "15:30",
            "instruments": instrument_codes,
        },
        ttl_seconds=60,
    )
