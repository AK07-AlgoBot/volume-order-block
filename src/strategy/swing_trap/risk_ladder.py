from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd

from strategy.swing_trap.models import LotExit
from strategy.swing_trap.session_clock import IST

if TYPE_CHECKING:
    from strategy.swing_trap.config import SwingTrapConfig


@dataclass
class LadderSimResult:
    lot_exits: list[LotExit]
    total_points: float
    exit_reason: str
    exit_ts: datetime
    final_price: float


def _row_ts(row: pd.Series) -> datetime:
    t = pd.to_datetime(row["timestamp"], errors="coerce")
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    return t.to_pydatetime()


def simulate_long_ladder(
    *,
    entry_price: float,
    stop_loss: float,
    target_full: float,
    cfg: SwingTrapConfig,
    path_df: pd.DataFrame,
    entry_bar_idx: int,
    force_exit_time: datetime,
) -> LadderSimResult:
    """
    Long: 2 lots @ 1R, 1 @ 2R, 1 @ 3R (target_full). Stops: initial, BE, then lock at 1R for last lot.
    Same bar: take-profit levels before trailing / secondary stops when both could trigger.
    """
    e = float(entry_price)
    sl0 = float(stop_loss)
    t3 = float(target_full)
    R = e - sl0
    if R <= 0:
        return LadderSimResult([], 0.0, "INVALID_RISK", force_exit_time, e)
    t1 = e + R
    t2 = e + 2.0 * R

    fe = force_exit_time.astimezone(IST) if force_exit_time.tzinfo else force_exit_time.replace(tzinfo=IST)
    sub = path_df.iloc[entry_bar_idx + 1 :].copy()
    exits: list[LotExit] = []
    total_pts = 0.0
    lots_left = int(cfg.total_lots)
    phase = 0

    if sub.empty:
        for i in range(lots_left):
            exits.append(LotExit(i + 1, fe, e, 0.0, "FORCE_EOD"))
        return LadderSimResult(exits, 0.0, "FORCE_EOD", fe, e)

    for _, row in sub.iterrows():
        lo = float(row["low"])
        hi = float(row["high"])
        ts = _row_ts(row)
        if ts >= fe:
            break

        # --- Take profit first (when multiple conditions could fire) ---
        if phase == 2 and lots_left == 1 and hi >= t3:
            exits.append(LotExit(len(exits) + 1, ts, t3, t3 - e, "TP3_PARTIAL"))
            total_pts += t3 - e
            return LadderSimResult(exits, total_pts, "TP3_FULL", ts, t3)

        if phase == 1 and lots_left == 2 and hi >= t2:
            exits.append(LotExit(len(exits) + 1, ts, t2, t2 - e, "TP2_PARTIAL"))
            total_pts += t2 - e
            lots_left -= 1
            phase = 2
            if phase == 2 and lots_left == 1 and hi >= t3:
                exits.append(LotExit(len(exits) + 1, ts, t3, t3 - e, "TP3_PARTIAL"))
                total_pts += t3 - e
                return LadderSimResult(exits, total_pts, "TP3_FULL", ts, t3)

        if phase == 0 and lots_left == 4 and hi >= t1:
            for _ in range(int(cfg.lots_at_1r)):
                exits.append(LotExit(len(exits) + 1, ts, t1, t1 - e, "TP1_PARTIAL"))
                total_pts += t1 - e
                lots_left -= 1
            phase = 1
            if phase == 1 and lots_left == 2 and hi >= t2:
                exits.append(LotExit(len(exits) + 1, ts, t2, t2 - e, "TP2_PARTIAL"))
                total_pts += t2 - e
                lots_left -= 1
                phase = 2
            if phase == 2 and lots_left == 1 and hi >= t3:
                exits.append(LotExit(len(exits) + 1, ts, t3, t3 - e, "TP3_PARTIAL"))
                total_pts += t3 - e
                return LadderSimResult(exits, total_pts, "TP3_FULL", ts, t3)

        # --- Stops ---
        if phase == 0:
            hit_sl = lo <= sl0
        elif phase == 1:
            hit_sl = lo < e
        else:
            hit_sl = lo < t1
        if hit_sl:
            px = sl0 if phase == 0 else (e if phase == 1 else t1)
            k0 = len(exits)
            for k in range(lots_left):
                exits.append(LotExit(k0 + k + 1, ts, px, px - e, "SL_HIT"))
                total_pts += px - e
            return LadderSimResult(exits, total_pts, "SL_HIT", ts, px)

    px = float(sub.iloc[-1]["close"])
    while lots_left > 0:
        exits.append(LotExit(len(exits) + 1, fe, px, px - e, "FORCE_EOD"))
        total_pts += px - e
        lots_left -= 1
    return LadderSimResult(exits, total_pts, "FORCE_EOD", fe, px)


def simulate_short_ladder(
    *,
    entry_price: float,
    stop_loss: float,
    target_full: float,
    cfg: SwingTrapConfig,
    path_df: pd.DataFrame,
    entry_bar_idx: int,
    force_exit_time: datetime,
) -> LadderSimResult:
    e = float(entry_price)
    sl0 = float(stop_loss)
    t3 = float(target_full)
    R = sl0 - e
    if R <= 0:
        return LadderSimResult([], 0.0, "INVALID_RISK", force_exit_time, e)
    t1 = e - R
    t2 = e - 2.0 * R
    fe = force_exit_time.astimezone(IST) if force_exit_time.tzinfo else force_exit_time.replace(tzinfo=IST)
    sub = path_df.iloc[entry_bar_idx + 1 :].copy()
    exits: list[LotExit] = []
    total_pts = 0.0
    lots_left = int(cfg.total_lots)
    phase = 0

    if sub.empty:
        for i in range(lots_left):
            exits.append(LotExit(i + 1, fe, e, 0.0, "FORCE_EOD"))
        return LadderSimResult(exits, 0.0, "FORCE_EOD", fe, e)

    for _, row in sub.iterrows():
        lo = float(row["low"])
        hi = float(row["high"])
        ts = _row_ts(row)
        if ts >= fe:
            break

        if phase == 2 and lots_left == 1 and lo <= t3:
            exits.append(LotExit(len(exits) + 1, ts, t3, e - t3, "TP3_PARTIAL"))
            total_pts += e - t3
            return LadderSimResult(exits, total_pts, "TP3_FULL", ts, t3)

        if phase == 1 and lots_left == 2 and lo <= t2:
            exits.append(LotExit(len(exits) + 1, ts, t2, e - t2, "TP2_PARTIAL"))
            total_pts += e - t2
            lots_left -= 1
            phase = 2
            if phase == 2 and lots_left == 1 and lo <= t3:
                exits.append(LotExit(len(exits) + 1, ts, t3, e - t3, "TP3_PARTIAL"))
                total_pts += e - t3
                return LadderSimResult(exits, total_pts, "TP3_FULL", ts, t3)

        if phase == 0 and lots_left == 4 and lo <= t1:
            for _ in range(int(cfg.lots_at_1r)):
                exits.append(LotExit(len(exits) + 1, ts, t1, e - t1, "TP1_PARTIAL"))
                total_pts += e - t1
                lots_left -= 1
            phase = 1
            if phase == 1 and lots_left == 2 and lo <= t2:
                exits.append(LotExit(len(exits) + 1, ts, t2, e - t2, "TP2_PARTIAL"))
                total_pts += e - t2
                lots_left -= 1
                phase = 2
            if phase == 2 and lots_left == 1 and lo <= t3:
                exits.append(LotExit(len(exits) + 1, ts, t3, e - t3, "TP3_PARTIAL"))
                total_pts += e - t3
                return LadderSimResult(exits, total_pts, "TP3_FULL", ts, t3)

        if phase == 0:
            hit_sl = hi >= sl0
        elif phase == 1:
            hit_sl = hi > e
        else:
            hit_sl = hi > t1
        if hit_sl:
            px = sl0 if phase == 0 else (e if phase == 1 else t1)
            k0 = len(exits)
            for k in range(lots_left):
                exits.append(LotExit(k0 + k + 1, ts, px, e - px, "SL_HIT"))
                total_pts += e - px
            return LadderSimResult(exits, total_pts, "SL_HIT", ts, px)

    px = float(sub.iloc[-1]["close"])
    while lots_left > 0:
        exits.append(LotExit(len(exits) + 1, fe, px, e - px, "FORCE_EOD"))
        total_pts += e - px
        lots_left -= 1
    return LadderSimResult(exits, total_pts, "FORCE_EOD", fe, px)
