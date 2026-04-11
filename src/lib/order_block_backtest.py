"""
Walk-forward backtest for order_block_logic: replay snapshots at each session end.

Uses the same analyze_pack() as the dashboard; no separate "backtest engine" rules.

Outcome stats (optional): compare signal-day daily close to the *next trading session*
daily close — buy wins if next close > signal close; sell wins if next close < signal close.
Ties (unchanged close) are counted as scratch and excluded from win_rate denominator.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from order_block_logic import Candle, analyze_pack

IST = ZoneInfo("Asia/Kolkata")


def parse_candle_dt(ts: str) -> datetime:
    s = (ts or "").strip()
    if not s:
        return datetime.min.replace(tzinfo=IST)
    if s.endswith("+0530"):
        s = s[:-5] + "+05:30"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def session_end_ist(d: date) -> datetime:
    """Cash market regular session end approximation (15:30 IST)."""
    return datetime.combine(d, time(15, 30), tzinfo=IST)


def truncate_candles(candles: list[Candle], end: datetime) -> list[Candle]:
    out: list[Candle] = []
    for c in candles:
        if parse_candle_dt(c.ts) <= end:
            out.append(c)
    return out


def trading_days_between(daily: list[Candle], start: date, end: date) -> list[date]:
    seen: set[date] = set()
    days: list[date] = []
    for c in daily:
        try:
            d = date.fromisoformat(c.ts[:10])
        except ValueError:
            continue
        if start <= d <= end and d not in seen:
            seen.add(d)
            days.append(d)
    return sorted(days)


def snapshot_analyze(
    daily: list[Candle],
    h60: list[Candle],
    m30: list[Candle],
    m15: list[Candle],
    m5: list[Candle],
    as_of: datetime,
) -> dict[str, Any] | None:
    d = truncate_candles(daily, as_of)
    h = truncate_candles(h60, as_of)
    t30 = truncate_candles(m30, as_of)
    t15 = truncate_candles(m15, as_of)
    f = truncate_candles(m5, as_of)
    if len(d) < 5:
        return None
    try:
        return analyze_pack(d, h, t30, t15, f)
    except Exception:
        return None


@dataclass
class DirectionStats:
    buy: int = 0
    sell: int = 0
    none_: int = 0
    probs_buy: list[float] = field(default_factory=list)
    probs_sell: list[float] = field(default_factory=list)

    def record(self, direction: str, prob: Any) -> None:
        p = float(prob) if prob is not None else None
        if direction == "buy":
            self.buy += 1
            if p is not None:
                self.probs_buy.append(p)
        elif direction == "sell":
            self.sell += 1
            if p is not None:
                self.probs_sell.append(p)
        else:
            # "none", "mixed", or unknown
            self.none_ += 1

    def avg_buy(self) -> float | None:
        return sum(self.probs_buy) / len(self.probs_buy) if self.probs_buy else None

    def avg_sell(self) -> float | None:
        return sum(self.probs_sell) / len(self.probs_sell) if self.probs_sell else None

    def avg_signal(self) -> float | None:
        all_p = self.probs_buy + self.probs_sell
        return sum(all_p) / len(all_p) if all_p else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "buy": self.buy,
            "sell": self.sell,
            "none": self.none_,
            "avg_probability_buy": round(self.avg_buy(), 2) if self.avg_buy() is not None else None,
            "avg_probability_sell": round(self.avg_sell(), 2) if self.avg_sell() is not None else None,
            "avg_probability_when_buy_or_sell": round(self.avg_signal(), 2) if self.avg_signal() is not None else None,
        }


def merge_stats(a: DirectionStats, b: DirectionStats) -> DirectionStats:
    return DirectionStats(
        buy=a.buy + b.buy,
        sell=a.sell + b.sell,
        none_=a.none_ + b.none_,
        probs_buy=a.probs_buy + b.probs_buy,
        probs_sell=a.probs_sell + b.probs_sell,
    )


def daily_close_series(daily: list[Candle]) -> list[tuple[date, float]]:
    """One row per calendar day (last close wins if duplicates). Chronologically sorted."""
    by_d: dict[date, float] = {}
    for c in daily:
        try:
            d = date.fromisoformat(c.ts[:10])
        except ValueError:
            continue
        by_d[d] = c.close
    return sorted(by_d.items(), key=lambda x: x[0])


def next_session_closes(
    series: list[tuple[date, float]], signal_day: date
) -> tuple[float, float] | None:
    """(close on signal_day, close on next trading day), or None if unavailable."""
    dates = [x[0] for x in series]
    i = bisect.bisect_left(dates, signal_day)
    if i >= len(dates) or dates[i] != signal_day:
        return None
    if i + 1 >= len(series):
        return None
    return series[i][1], series[i + 1][1]


def classify_next_session(direction: str, entry_close: float, next_close: float) -> str:
    """win | loss | scratch (flat). Expects direction in buy/sell."""
    if direction == "buy":
        if next_close > entry_close:
            return "win"
        if next_close < entry_close:
            return "loss"
        return "scratch"
    if direction == "sell":
        if next_close < entry_close:
            return "win"
        if next_close > entry_close:
            return "loss"
        return "scratch"
    raise ValueError("direction must be buy or sell")


@dataclass
class OutcomeStats:
    """Empirical next-session stats for buy/sell signals only."""

    wins: int = 0
    losses: int = 0
    scratches: int = 0
    skipped_no_next_bar: int = 0

    def record(
        self,
        direction: str,
        closes: tuple[float, float] | None,
    ) -> None:
        if direction not in ("buy", "sell"):
            return
        if closes is None:
            self.skipped_no_next_bar += 1
            return
        entry, nxt = closes
        o = classify_next_session(direction, entry, nxt)
        if o == "win":
            self.wins += 1
        elif o == "loss":
            self.losses += 1
        else:
            self.scratches += 1

    def win_rate(self) -> float | None:
        d = self.wins + self.losses
        return self.wins / d if d else None

    def to_dict(self) -> dict[str, Any]:
        wr = self.win_rate()
        decided = self.wins + self.losses
        return {
            "wins": self.wins,
            "losses": self.losses,
            "scratches": self.scratches,
            "skipped_no_next_bar": self.skipped_no_next_bar,
            "signals_with_next_session": self.wins + self.losses + self.scratches,
            "win_rate_next_session": round(wr, 4) if wr is not None else None,
            "note": "win_rate = wins / (wins + losses); scratches excluded",
        }


def merge_outcomes(a: OutcomeStats, b: OutcomeStats) -> OutcomeStats:
    return OutcomeStats(
        wins=a.wins + b.wins,
        losses=a.losses + b.losses,
        scratches=a.scratches + b.scratches,
        skipped_no_next_bar=a.skipped_no_next_bar + b.skipped_no_next_bar,
    )
