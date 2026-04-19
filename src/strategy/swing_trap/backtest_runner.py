from __future__ import annotations

from datetime import datetime, timedelta
import pandas as pd

from strategy.swing_trap.config import SwingTrapConfig
from strategy.swing_trap.entry_trap import find_trap_entry_long, find_trap_entry_short
from strategy.swing_trap.models import SwingTrapTrade
from strategy.swing_trap.risk_ladder import simulate_long_ladder, simulate_short_ladder
from strategy.swing_trap.session_clock import (
    IST,
    force_exit_deadline,
    last_entry_allowed,
    trading_day_for_ts,
)
from strategy.swing_trap.swing_30m import (
    Swing30mSeries,
    nearest_pivot_high_above,
    nearest_pivot_low_below,
    prepare_30m_df,
)


def _coerce_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["timestamp", "open", "high", "low", "close"]).sort_values("timestamp").reset_index(drop=True)


def _bar_end_ts(row: pd.Series, minutes: int) -> datetime:
    t = pd.to_datetime(row["timestamp"], errors="coerce")
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    else:
        t = t.tz_convert(IST)
    return (t + timedelta(minutes=minutes)).to_pydatetime()


def run_swing_trap_backtest(
    df1: pd.DataFrame,
    df5: pd.DataFrame,
    df30: pd.DataFrame,
    cfg: SwingTrapConfig,
) -> list[SwingTrapTrade]:
    """
    Walk 5m bars in time order; detect breakout -> retest -> trap -> entry; simulate 4-lot ladder.
    """
    df1 = _coerce_df(df1) if not df1.empty else df1
    df5 = _coerce_df(df5)
    df30 = prepare_30m_df(df30)
    swing = Swing30mSeries(df30, cfg)

    trades: list[SwingTrapTrade] = []
    n = len(df5)
    cooldown_until = -1

    i = 1
    while i < n - 1:
        if i <= cooldown_until:
            i += 1
            continue

        row = df5.iloc[i]
        prev = df5.iloc[i - 1]
        t_end = _bar_end_ts(row, 5)
        day = trading_day_for_ts(t_end)
        no_entry_after = last_entry_allowed(day, cfg.no_new_entries_after_hms)
        force_exit_at = force_exit_deadline(day, cfg.force_exit_open_positions_by_hms)

        if t_end > no_entry_after:
            i += 1
            continue

        snap_prev = swing.snapshot_at(_bar_end_ts(prev, 5))
        if snap_prev is None:
            i += 1
            continue

        c = float(row["close"])
        c0 = float(prev["close"])
        sh = float(snap_prev.session_high)
        sl_ = float(snap_prev.session_low)

        long_break = c > sh and c0 <= sh
        short_break = c < sl_ and c0 >= sl_

        if not long_break and not short_break:
            i += 1
            continue

        setup = None
        if long_break:
            setup = find_trap_entry_long(
                df5,
                df1 if not df1.empty else None,
                i,
                sh,
                max_retest_to_trap=cfg.max_bars_retest_to_trap,
                require_green_confirm=cfg.require_confirm_body_direction,
                use_1m_fallback=cfg.use_1m_trap_fallback,
                ambiguous_wick_ratio=float(cfg.ambiguous_if_wick_ratio_gt),
            )
        elif short_break:
            setup = find_trap_entry_short(
                df5,
                df1 if not df1.empty else None,
                i,
                sl_,
                max_retest_to_trap=cfg.max_bars_retest_to_trap,
                require_red_confirm=cfg.require_confirm_body_direction,
                use_1m_fallback=cfg.use_1m_trap_fallback,
                ambiguous_wick_ratio=float(cfg.ambiguous_if_wick_ratio_gt),
            )

        if setup is None:
            i += 1
            continue

        entry_idx = setup.entry_idx
        entry_row = df5.iloc[entry_idx]
        entry_px = float(entry_row["close"])
        entry_ts = _bar_end_ts(entry_row, 5)

        day_df30 = swing.day_df(day)
        if day_df30 is None or day_df30.empty:
            i += 1
            continue

        max_ts = pd.Timestamp(entry_ts)
        if setup.side == "LONG":
            T = nearest_pivot_high_above(day_df30, entry_px, max_ts)
            if T is None:
                T = float(entry_px) + float(cfg.default_target_fallback_rr) * max(float(entry_px) - sh, 1.0)
            reward = float(T) - float(entry_px)
            risk = reward / float(cfg.reward_risk_multiple)
            sl_px = float(entry_px) - risk
        else:
            T = nearest_pivot_low_below(day_df30, entry_px, max_ts)
            if T is None:
                T = float(entry_px) - float(cfg.default_target_fallback_rr) * max(sl_ - float(entry_px), 1.0)
            reward = float(entry_px) - float(T)
            risk = reward / float(cfg.reward_risk_multiple)
            sl_px = float(entry_px) + risk

        if setup.side == "LONG":
            sim = simulate_long_ladder(
                entry_price=entry_px,
                stop_loss=sl_px,
                target_full=float(T),
                cfg=cfg,
                path_df=df5,
                entry_bar_idx=entry_idx,
                force_exit_time=force_exit_at,
            )
        else:
            sim = simulate_short_ladder(
                entry_price=entry_px,
                stop_loss=sl_px,
                target_full=float(T),
                cfg=cfg,
                path_df=df5,
                entry_bar_idx=entry_idx,
                force_exit_time=force_exit_at,
            )

        trades.append(
            SwingTrapTrade(
                side=setup.side,
                entry_tf=cfg.tf_entry,
                entry_ts=entry_ts,
                entry_price=entry_px,
                exit_ts=sim.exit_ts,
                swing_high_ref=sh,
                swing_low_ref=sl_,
                breakout_level=setup.level,
                stop_loss=sl_px,
                target_price=float(T),
                risk_per_unit=abs(entry_px - sl_px),
                reward_per_unit=abs(float(T) - entry_px),
                total_points=sim.total_points,
                exit_reason=sim.exit_reason,
                lot_exits=sim.lot_exits,
                meta={
                    "breakout_idx": setup.breakout_idx,
                    "retest_idx": setup.retest_idx,
                    "trap_idx": setup.trap_idx,
                    "entry_idx": entry_idx,
                    "used_1m_confirm": setup.used_1m_confirm,
                },
            )
        )

        cooldown_until = max(cooldown_until, entry_idx + int(cfg.second_path_min_separation_bars))
        i = entry_idx + 1

    return trades
