"""Unit tests for 4-lot ladder simulation (no broker / no Kite)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategy.swing_trap.config import SwingTrapConfig
from strategy.swing_trap.risk_ladder import simulate_long_ladder
from strategy.swing_trap.session_clock import IST


def _ts(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    out = []
    for t, o, h, l in rows:
        out.append({"timestamp": pd.Timestamp(t, tz=IST), "open": o, "high": h, "low": l, "close": (h + l) / 2})
    return pd.DataFrame(out)


def test_long_hits_tp3_all_partials() -> None:
    cfg = SwingTrapConfig(total_lots=4, lots_at_1r=2, lots_at_2r=1, lots_at_3r=1)
    e, sl0 = 100.0, 97.0
    R = 3.0
    t1, t2, t3 = e + R, e + 2 * R, e + 3 * R
    # Single bar must reach full TP3 high; lows stay at entry so BE / trail are not hit before TP.
    path = _ts([("2026-01-01 10:00:00", e, t3 + 1.0, e + 0.5)])
    fe = datetime(2026, 1, 1, 15, 19, tzinfo=IST)
    sim = simulate_long_ladder(
        entry_price=e,
        stop_loss=sl0,
        target_full=t3,
        cfg=cfg,
        path_df=path,
        entry_bar_idx=-1,
        force_exit_time=fe,
    )
    assert sim.exit_reason == "TP3_FULL"
    assert len(sim.lot_exits) == 4
    # 2 lots @ 1R + 1 @ 2R + 1 @ 3R
    assert abs(sim.total_points - (2 * R + 2 * R + 3 * R)) < 1e-6


def test_long_stopped_at_initial_sl() -> None:
    cfg = SwingTrapConfig()
    e, sl0 = 100.0, 97.0
    path = _ts([("2026-01-01 10:00:00", e, e + 1, sl0 - 0.5)])
    fe = datetime(2026, 1, 1, 15, 19, tzinfo=IST)
    sim = simulate_long_ladder(
        entry_price=e,
        stop_loss=sl0,
        target_full=109.0,
        cfg=cfg,
        path_df=path,
        entry_bar_idx=-1,
        force_exit_time=fe,
    )
    assert sim.exit_reason == "SL_HIT"
    assert len(sim.lot_exits) == 4
