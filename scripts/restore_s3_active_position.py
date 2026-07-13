#!/usr/bin/env python3
"""Restore / enable active S3 breakout position in Redis (dashboard + engine).

The engine only restores positions that include tp2_price, lot_size, etc.
Partial session blobs (e.g. after a manual TP patch) fail silently → engine flat.

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/restore_s3_active_position.py NIFTY

  docker compose -p ak07 -f configs/docker-compose.yml restart breakout_engine
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services import cache_manager  # noqa: E402
from app.services.breakout_engine import (  # noqa: E402
    FIXED_SL_PTS,
    FIXED_TP_PTS,
    INDEX_CONFIGS,
    IST,
    _position_from_dict,
    trade_levels,
)

TTL = 86_400 * 2


def _normalize_position(pos: dict[str, Any], *, index_code: str, session_date) -> dict[str, Any]:
    direction = str(pos.get("direction") or "").upper()
    entry = float(pos.get("entry_price") or 0)
    if direction not in ("LONG", "SHORT") or entry <= 0:
        raise ValueError("position needs direction LONG|SHORT and entry_price")

    cfg = INDEX_CONFIGS[index_code]
    legs = [leg for leg in (pos.get("order_legs") or []) if isinstance(leg, dict)]
    instrument_key = str(pos.get("instrument_key") or "")
    contract_label = str(pos.get("contract_label") or "")
    if not instrument_key and legs:
        instrument_key = str(legs[0].get("instrument_key") or "")
    if not contract_label and legs:
        contract_label = str(
            legs[0].get("contract_label") or legs[0].get("trading_symbol") or f"{index_code} FUT"
        )

    sl, tp1, tp2 = trade_levels(
        index_code,
        direction,
        entry,
        mid=entry,
        green=entry,
        red=entry,
        gap_regime="",
        session_date=session_date,
    )
    if pos.get("sl_price") is not None:
        sl = float(pos["sl_price"])
    if pos.get("tp1_price") is not None:
        tp1 = float(pos["tp1_price"])
    if pos.get("tp2_price") is not None:
        tp2 = float(pos["tp2_price"])

    return {
        "direction": direction,
        "entry_price": entry,
        "sl_price": sl,
        "tp1_price": tp1,
        "tp2_price": tp2,
        "lot_size": int(pos.get("lot_size") or cfg.lot_size),
        "instrument_key": instrument_key,
        "contract_label": contract_label,
        "order_legs": legs,
        "option_strike": int(pos.get("option_strike") or 0),
        "option_type": str(pos.get("option_type") or ""),
        "opened_at": str(pos.get("opened_at") or datetime.now(IST).isoformat()),
        "entry_reason": str(pos.get("entry_reason") or "manual restore — active position"),
        "exit_pending": False,
    }


def main() -> int:
    index_code = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").strip().upper()
    if index_code not in INDEX_CONFIGS:
        print(f"Unknown index {index_code}")
        return 1

    today = datetime.now(IST).date()
    state_key = cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code)
    session_key = cache_manager.BREAKOUT_SESSION_KEY_TEMPLATE.format(day=today.isoformat(), index=index_code)

    session = cache_manager.get_json(session_key) or {}
    raw = cache_manager.get_json(state_key) or {}
    if not isinstance(session, dict):
        session = {}
    if not isinstance(raw, dict):
        raw = {}

    pos = session.get("position")
    if not isinstance(pos, dict):
        pos = raw.get("position") if isinstance(raw.get("position"), dict) else None
    if not isinstance(pos, dict):
        print(f"No position found in session or state for {index_code}.")
        return 1

    full = _normalize_position(pos, index_code=index_code, session_date=today)
    parsed = _position_from_dict(full)
    if parsed is None:
        print("ERROR — normalized position still fails engine parse:")
        print(full)
        return 1

    trades_today = int(session.get("trades_today") or raw.get("trades_today") or 2)
    logs = session.get("signal_log") if isinstance(session.get("signal_log"), list) else []
    note = (
        f"{index_code} active position restored in Redis "
        f"{full['direction']} @ {full['entry_price']:.2f} "
        f"SL {full['sl_price']:.2f} TP1 {full['tp1_price']:.2f} (Friday 1:1)"
    )
    logs = list(logs) + [note]

    session["position"] = full
    session["trades_today"] = trades_today
    session["signal_log"] = logs[-20:]
    cache_manager.set_json(session_key, session, ttl_seconds=TTL)

    raw["position"] = full
    raw["trades_today"] = trades_today
    raw["signals"] = logs[-10:]
    cache_manager.set_json(state_key, raw, ttl_seconds=86_400)

    print(f"OK — {note}")
    print(f"  tp2: {full['tp2_price']:.2f}  lot_size: {full['lot_size']}")
    print(f"  legs: {[leg.get('username') for leg in full['order_legs']]}")
    print("\nRestart breakout_engine:")
    print("  docker compose -p ak07 -f configs/docker-compose.yml restart breakout_engine")
    print("\nVerify (expect restored LONG, TP1 24212.55):")
    print("  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\")
    print("    python scripts/check_s3_position_state.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
