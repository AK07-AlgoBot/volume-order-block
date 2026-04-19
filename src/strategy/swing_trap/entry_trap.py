from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd

from strategy.swing_trap.session_clock import IST

Side = Literal["LONG", "SHORT"]


@dataclass
class TrapSetupResult:
    side: Side
    breakout_idx: int
    retest_idx: int
    trap_idx: int
    entry_idx: int
    level: float
    used_1m_confirm: bool


def _bar_end(ts: pd.Timestamp, minutes: int) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    else:
        t = t.tz_convert(IST)
    return t + timedelta(minutes=minutes)


def _body_ratio(open_: float, high: float, low: float, close: float) -> float:
    rng = high - low
    if rng <= 0:
        return 0.0
    return abs(close - open_) / rng


def find_trap_entry_long(
    df5: pd.DataFrame,
    df1: pd.DataFrame | None,
    breakout_idx: int,
    level: float,
    *,
    max_retest_to_trap: int,
    require_green_confirm: bool,
    use_1m_fallback: bool,
    ambiguous_wick_ratio: float,
) -> TrapSetupResult | None:
    """
    After breakout at breakout_idx (close > level), scan forward for retest -> trap -> confirm.
    Entry on confirming bar close.
    """
    n = len(df5)
    j = None
    cap = min(n - 1, breakout_idx + max_retest_to_trap)
    for a in range(breakout_idx + 1, cap + 1):
        lo = float(df5.iloc[a]["low"])
        hi = float(df5.iloc[a]["high"])
        if lo <= level <= hi or lo <= level:
            j = a
            break
    if j is None:
        return None

    trap_k = None
    for b in range(j + 1, min(n, j + 1 + max_retest_to_trap)):
        if float(df5.iloc[b]["low"]) < level:
            trap_k = b
            break
    if trap_k is None:
        return None

    for c in range(trap_k + 1, n):
        row = df5.iloc[c]
        cl = float(row["close"])
        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        if cl <= level:
            continue
        if require_green_confirm and cl <= op:
            continue
        ambiguous = _body_ratio(op, hi, lo, cl) < (1.0 - ambiguous_wick_ratio)
        used_1m = False
        if ambiguous and use_1m_fallback and df1 is not None:
            if not _confirm_trap_1m_long(df1, df5, trap_k, c, level):
                continue
            used_1m = True
        elif ambiguous:
            continue

        return TrapSetupResult(
            side="LONG",
            breakout_idx=breakout_idx,
            retest_idx=j,
            trap_idx=trap_k,
            entry_idx=c,
            level=level,
            used_1m_confirm=used_1m,
        )
    return None


def find_trap_entry_short(
    df5: pd.DataFrame,
    df1: pd.DataFrame | None,
    breakout_idx: int,
    level: float,
    *,
    max_retest_to_trap: int,
    require_red_confirm: bool,
    use_1m_fallback: bool,
    ambiguous_wick_ratio: float,
) -> TrapSetupResult | None:
    n = len(df5)
    j = None
    cap = min(n - 1, breakout_idx + max_retest_to_trap)
    for a in range(breakout_idx + 1, cap + 1):
        lo = float(df5.iloc[a]["low"])
        hi = float(df5.iloc[a]["high"])
        if lo <= level <= hi or hi >= level:
            j = a
            break
    if j is None:
        return None

    trap_k = None
    for b in range(j + 1, min(n, j + 1 + max_retest_to_trap)):
        if float(df5.iloc[b]["high"]) > level:
            trap_k = b
            break
    if trap_k is None:
        return None

    for c in range(trap_k + 1, n):
        row = df5.iloc[c]
        cl = float(row["close"])
        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        if cl >= level:
            continue
        if require_red_confirm and cl >= op:
            continue
        ambiguous = _body_ratio(op, hi, lo, cl) < (1.0 - ambiguous_wick_ratio)
        used_1m = False
        if ambiguous and use_1m_fallback and df1 is not None:
            if not _confirm_trap_1m_short(df1, df5, trap_k, c, level):
                continue
            used_1m = True
        elif ambiguous:
            continue

        return TrapSetupResult(
            side="SHORT",
            breakout_idx=breakout_idx,
            retest_idx=j,
            trap_idx=trap_k,
            entry_idx=c,
            level=level,
            used_1m_confirm=used_1m,
        )
    return None


def _confirm_trap_1m_long(
    df1: pd.DataFrame,
    df5: pd.DataFrame,
    trap_k: int,
    confirm_c: int,
    level: float,
) -> bool:
    t0 = _bar_end(pd.to_datetime(df5.iloc[trap_k]["timestamp"]), 5)
    t1 = _bar_end(pd.to_datetime(df5.iloc[confirm_c]["timestamp"]), 5)
    ts = pd.to_datetime(df1["timestamp"], errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    mask = (ts >= t0) & (ts < t1)
    sub = df1.loc[mask]
    if sub.empty:
        return False
    violated = (sub["low"].astype(float) < level).any()
    if not violated:
        return False
    last = sub.iloc[-1]
    return float(last["close"]) > level


def _confirm_trap_1m_short(
    df1: pd.DataFrame,
    df5: pd.DataFrame,
    trap_k: int,
    confirm_c: int,
    level: float,
) -> bool:
    t0 = _bar_end(pd.to_datetime(df5.iloc[trap_k]["timestamp"]), 5)
    t1 = _bar_end(pd.to_datetime(df5.iloc[confirm_c]["timestamp"]), 5)
    ts = pd.to_datetime(df1["timestamp"], errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    mask = (ts >= t0) & (ts < t1)
    sub = df1.loc[mask]
    if sub.empty:
        return False
    violated = (sub["high"].astype(float) > level).any()
    if not violated:
        return False
    last = sub.iloc[-1]
    return float(last["close"]) < level
