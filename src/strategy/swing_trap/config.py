from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SwingTrapConfig:
    """Tunable parameters for swing/trap strategy and backtest."""

    # Timeframes (Kite names where applicable)
    tf_swing: str = "30minute"
    tf_entry: str = "5minute"
    tf_confirm: str = "minute"

    # Session (IST) — NSE index futures style
    market_open_hms: tuple[int, int, int] = (9, 15, 0)
    market_close_hms: tuple[int, int, int] = (15, 30, 0)
    no_new_entries_after_hms: tuple[int, int, int] = (15, 0, 0)
    force_exit_open_positions_by_hms: tuple[int, int, int] = (15, 19, 0)

    # Swing reference for breakouts (30m data):
    # - "prior_day_30m_range": each day, trade only breaks of previous session's 30m high/low (fixed at day start).
    # - "rolling_session_30m": legacy — intraday running high/low of completed 30m bars (expands through the day).
    daily_breakout_reference: str = "prior_day_30m_range"
    use_session_extrema_swings: bool = True
    pivot_lookback_bars: int = 2

    # Retest / trap (5m)
    max_bars_retest_to_trap: int = 18
    max_bars_breakout_to_abandon: int = 36
    require_confirm_body_direction: bool = True

    # Optional 1m confirmation when 5m trap is ambiguous
    use_1m_trap_fallback: bool = True
    ambiguous_if_wick_ratio_gt: float = 0.55

    # Risk: reward_multiple : 1 means reward = multiple * risk (e.g. 3 -> 1:3)
    reward_risk_multiple: float = 3.0
    default_target_fallback_rr: float = 3.0

    # Multi-lot (4 equal units)
    total_lots: int = 4
    lots_at_1r: int = 2
    lots_at_2r: int = 1
    lots_at_3r: int = 1

    # Second-chance swing refresh (simplified)
    enable_second_entry_path: bool = True
    second_path_min_separation_bars: int = 3
