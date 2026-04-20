from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import pandas as pd

from strategy.swing_trap.config import SwingTrapConfig
from strategy.swing_trap.session_clock import IST


@dataclass
class Swing30mSnapshot:
    """Swing levels known as-of a decision time (5m bar end)."""

    asof: datetime
    trading_day: date
    session_high: float
    session_low: float
    last_completed_30m_end: datetime | None


def _session_mask(df: pd.DataFrame, day: date) -> pd.Series:
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    return ts.dt.date == day


def prepare_30m_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["high", "low", "timestamp"])
    out = out.sort_values("timestamp").reset_index(drop=True)
    return out


def pivot_highs(
    df: pd.DataFrame,
    left: int = 1,
    right: int = 1,
) -> pd.Series:
    """Boolean series: fractal pivot high at i."""
    h = df["high"].astype(float)
    n = len(df)
    out = pd.Series(False, index=df.index)
    for i in range(left, n - right):
        window = h.iloc[i - left : i + right + 1]
        if h.iloc[i] == window.max() and (h.iloc[i] > h.iloc[i - 1] or h.iloc[i] > h.iloc[i + 1]):
            out.iloc[i] = True
    return out


def pivot_lows(df: pd.DataFrame, left: int = 1, right: int = 1) -> pd.Series:
    lo = df["low"].astype(float)
    n = len(df)
    out = pd.Series(False, index=df.index)
    for i in range(left, n - right):
        window = lo.iloc[i - left : i + right + 1]
        if lo.iloc[i] == window.min() and (lo.iloc[i] < lo.iloc[i - 1] or lo.iloc[i] < lo.iloc[i + 1]):
            out.iloc[i] = True
    return out


def nearest_pivot_high_above(
    df: pd.DataFrame,
    entry_price: float,
    max_ts: pd.Timestamp,
) -> float | None:
    """Smallest pivot high strictly above entry among bars with end <= max_ts."""
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    end = ts + timedelta(minutes=30)
    mxt = pd.Timestamp(max_ts)
    if mxt.tzinfo is None:
        mxt = mxt.tz_localize(IST)
    else:
        mxt = mxt.tz_convert(IST)
    mask = end <= mxt
    sub = df.loc[mask].copy()
    if sub.empty:
        return None
    ph = pivot_highs(sub, 1, 1)
    highs = sub.loc[ph, "high"].astype(float)
    highs = highs[highs > float(entry_price)]
    if highs.empty:
        return None
    return float(highs.min())


def nearest_pivot_low_below(
    df: pd.DataFrame,
    entry_price: float,
    max_ts: pd.Timestamp,
) -> float | None:
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    end = ts + timedelta(minutes=30)
    mxt = pd.Timestamp(max_ts)
    if mxt.tzinfo is None:
        mxt = mxt.tz_localize(IST)
    else:
        mxt = mxt.tz_convert(IST)
    mask = end <= mxt
    sub = df.loc[mask].copy()
    if sub.empty:
        return None
    pl = pivot_lows(sub, 1, 1)
    lows = sub.loc[pl, "low"].astype(float)
    lows = lows[lows < float(entry_price)]
    if lows.empty:
        return None
    return float(lows.max())


class Swing30mSeries:
    """Pre-indexed 30m data for fast swing lookups during 5m walk."""

    def __init__(self, df30: pd.DataFrame, cfg: SwingTrapConfig) -> None:
        self.cfg = cfg
        self.df = prepare_30m_df(df30)
        self._by_day: dict[date, pd.DataFrame] = {}
        self._sorted_days: list[date] = []
        ts = pd.to_datetime(self.df["timestamp"], errors="coerce")
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(IST)
        else:
            ts = ts.dt.tz_convert(IST)
        self.df["_day"] = ts.dt.date
        for d, g in self.df.groupby("_day"):
            self._by_day[d] = g.drop(columns=["_day"]).reset_index(drop=True)
        self._sorted_days = sorted(self._by_day.keys())

    def day_df(self, day: date) -> pd.DataFrame | None:
        g = self._by_day.get(day)
        return g.copy() if g is not None else None

    def prior_trading_day(self, day: date) -> date | None:
        """Latest calendar day in 30m data strictly before `day` (previous session)."""
        prior = [d for d in self._sorted_days if d < day]
        return prior[-1] if prior else None

    def session_high_low_30m(self, day: date) -> tuple[float, float] | None:
        """Session range from 30m bars on that day: (max high, min low)."""
        g = self._by_day.get(day)
        if g is None or g.empty:
            return None
        return float(g["high"].max()), float(g["low"].min())

    def snapshot_at(self, decision_time: datetime) -> Swing30mSnapshot | None:
        """Session running high/low from all 30m bars fully closed before or at decision_time."""
        dt = decision_time.astimezone(IST) if decision_time.tzinfo else decision_time.replace(tzinfo=IST)
        day = dt.date()
        part = self._by_day.get(day)
        if part is None or part.empty:
            return None
        ts = pd.to_datetime(part["timestamp"], errors="coerce")
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(IST)
        else:
            ts = ts.dt.tz_convert(IST)
        end = ts + timedelta(minutes=30)
        dt_ts = pd.Timestamp(dt)
        if dt_ts.tzinfo is None:
            dt_ts = dt_ts.tz_localize(IST)
        else:
            dt_ts = dt_ts.tz_convert(IST)
        done = part.loc[end <= dt_ts]
        if done.empty:
            return None
        last_end = end.loc[done.index[-1]]
        return Swing30mSnapshot(
            asof=dt,
            trading_day=day,
            session_high=float(done["high"].max()),
            session_low=float(done["low"].min()),
            last_completed_30m_end=last_end.to_pydatetime(),
        )
