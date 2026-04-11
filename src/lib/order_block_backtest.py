"""
Walk-forward backtest for order_block_logic: replay snapshots at each session end.

Uses the same analyze_pack() as the dashboard; no separate "backtest engine" rules.
Output is signal frequency and average score — not P&L (would need forward returns).
"""

from __future__ import annotations

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
