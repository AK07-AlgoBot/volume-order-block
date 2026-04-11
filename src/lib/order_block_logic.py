"""
Swing / order-block style analysis from OHLC candles (used with Zerodha Kite historical data).

Rules (from product spec, implemented heuristically):
- Swing high at i: two candles to the left have highs not above swing high.
- Swing low at i: two candles to the left have lows not below swing low.
- Intraday: 30m swings + 5m break/retest; SL from recent 5m swing; target from opposing 30m swing.
- Positional: 60m + 15m with same pattern; trend from daily + weekly structure from daily.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def rows_to_candles(rows: list[list[Any]]) -> list[Candle]:
    out: list[Candle] = []
    for row in rows or []:
        if not row or len(row) < 6:
            continue
        ts = str(row[0])
        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        v = float(row[5]) if len(row) > 5 else 0.0
        out.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v))
    return out


def _is_swing_high(highs: list[float], i: int) -> bool:
    if i < 2 or i >= len(highs):
        return False
    h = highs[i]
    return h >= highs[i - 1] and h >= highs[i - 2]


def _is_swing_low(lows: list[float], i: int) -> bool:
    if i < 2 or i >= len(lows):
        return False
    lo = lows[i]
    return lo <= lows[i - 1] and lo <= lows[i - 2]


def last_swing_high_index(highs: list[float]) -> int | None:
    for i in range(len(highs) - 1, 1, -1):
        if _is_swing_high(highs, i):
            return i
    return None


def last_swing_low_index(lows: list[float]) -> int | None:
    for i in range(len(lows) - 1, 1, -1):
        if _is_swing_low(lows, i):
            return i
    return None


def last_swing_high_price(candles: list[Candle]) -> float | None:
    if len(candles) < 3:
        return None
    highs = [c.high for c in candles]
    ix = last_swing_high_index(highs)
    return candles[ix].high if ix is not None else None


def last_swing_low_price(candles: list[Candle]) -> float | None:
    if len(candles) < 3:
        return None
    lows = [c.low for c in candles]
    ix = last_swing_low_index(lows)
    return candles[ix].low if ix is not None else None


def daily_trend_bias(candles: list[Candle]) -> str:
    if len(candles) < 22:
        return "unknown"
    closes = [c.close for c in candles]
    sma20 = sum(closes[-20:]) / 20.0
    last = closes[-1]
    if last > sma20 * 1.005:
        return "bullish"
    if last < sma20 * 0.995:
        return "bearish"
    return "neutral"


def weekly_bias_from_daily(candles: list[Candle]) -> str:
    """Approximate weekly trend using last closes per ISO week."""
    if len(candles) < 15:
        return "unknown"
    from collections import defaultdict

    week_last: dict[tuple[int, int], float] = defaultdict(float)
    for c in candles:
        ts = c.ts[:10]
        try:
            y, m, d = int(ts[0:4]), int(ts[5:7]), int(ts[8:10])
        except (ValueError, IndexError):
            continue
        dt = date(y, m, d)
        iso = dt.isocalendar()
        key = (iso[0], iso[1])
        week_last[key] = c.close
    if len(week_last) < 3:
        return "unknown"
    keys = sorted(week_last.keys())
    last3 = [week_last[k] for k in keys[-3:]]
    if last3[-1] > last3[0] * 1.01:
        return "bullish"
    if last3[-1] < last3[0] * 0.99:
        return "bearish"
    return "neutral"


def _detect_buy_pattern(
    m_struct: list[Candle],
    m_trigger: list[Candle],
    eps_ratio: float = 0.0015,
) -> tuple[bool, str]:
    """
    Buy: 5m (or 15m) low breaks below structural swing low (30m/60m), then price returns to retest.
    """
    if len(m_struct) < 4 or len(m_trigger) < 10:
        return False, "insufficient candles"
    sl_ref = last_swing_low_price(m_struct)
    sh_ref = last_swing_high_price(m_struct)
    if sl_ref is None or sh_ref is None:
        return False, "could not locate structural swings"

    lows_t = [c.low for c in m_trigger]
    highs_t = [c.high for c in m_trigger]

    # Scan for break below 30m/60m swing low, then retest from below/into zone
    L = sl_ref
    broke_at: int | None = None
    for i in range(len(m_trigger) - 1, 4, -1):
        if lows_t[i] < L * (1.0 - eps_ratio):
            broke_at = i
            break
    if broke_at is None:
        return False, "no 5m/15m break below structural swing low yet"

    retest = False
    for j in range(broke_at + 1, len(m_trigger)):
        # price comes back toward swing low after reversal up
        if m_trigger[j].low <= L * (1.0 + eps_ratio * 3) and m_trigger[j].close >= L * (1.0 - eps_ratio * 5):
            retest = True
            break
    if not retest:
        return False, "break seen; waiting for retest of structural swing low"

    return True, "break + retest pattern detected (buy)"


def _detect_sell_pattern(
    m_struct: list[Candle],
    m_trigger: list[Candle],
    eps_ratio: float = 0.0015,
) -> tuple[bool, str]:
    if len(m_struct) < 4 or len(m_trigger) < 10:
        return False, "insufficient candles"
    sh_ref = last_swing_high_price(m_struct)
    sl_ref = last_swing_low_price(m_struct)
    if sh_ref is None or sl_ref is None:
        return False, "could not locate structural swings"
    H = sh_ref
    highs_t = [c.high for c in m_trigger]
    broke_at: int | None = None
    for i in range(len(m_trigger) - 1, 4, -1):
        if highs_t[i] > H * (1.0 + eps_ratio):
            broke_at = i
            break
    if broke_at is None:
        return False, "no 5m/15m break above structural swing high yet"
    for j in range(broke_at + 1, len(m_trigger)):
        if m_trigger[j].high >= H * (1.0 - eps_ratio * 3) and m_trigger[j].close <= H * (1.0 + eps_ratio * 5):
            return True, "break + retest pattern detected (sell)"
    return False, "break seen; waiting for retest of structural swing high"


def _recent_swing_low_5m(candles: list[Candle]) -> float | None:
    lows = [c.low for c in candles]
    ix = last_swing_low_index(lows)
    return candles[ix].low if ix is not None else None


def _recent_swing_high_5m(candles: list[Candle]) -> float | None:
    highs = [c.high for c in candles]
    ix = last_swing_high_index(highs)
    return candles[ix].high if ix is not None else None


def _probability(
    direction: str,
    daily_bias: str,
    weekly_bias: str,
    pattern_ok: bool,
) -> int:
    p = 42
    if pattern_ok:
        p += 28
    if direction == "buy":
        if daily_bias == "bullish":
            p += 12
        if weekly_bias == "bullish":
            p += 10
    elif direction == "sell":
        if daily_bias == "bearish":
            p += 12
        if weekly_bias == "bearish":
            p += 10
    else:
        p += 5
    return max(18, min(94, p))


def build_section(
    label: str,
    m_struct: list[Candle],
    m_trigger: list[Candle],
    daily_bias: str,
    weekly_bias: str,
) -> dict[str, Any]:
    """One section: intraday (30m+5m) or positional (60m+15m)."""
    sh = last_swing_high_price(m_struct)
    sl = last_swing_low_price(m_struct)
    buy_ok, buy_note = _detect_buy_pattern(m_struct, m_trigger)
    sell_ok, sell_note = _detect_sell_pattern(m_struct, m_trigger)

    direction = "none"
    if buy_ok and not sell_ok:
        direction = "buy"
    elif sell_ok and not buy_ok:
        direction = "sell"
    elif buy_ok and sell_ok:
        direction = "mixed"
        buy_note = buy_note + " / " + sell_note

    entry = sl if direction == "buy" else (sh if direction == "sell" else None)
    target = sh if direction == "buy" else (sl if direction == "sell" else None)
    if direction == "mixed":
        entry = None
        target = None

    sl_price = None
    if direction == "buy":
        sl_price = _recent_swing_low_5m(m_trigger)
    elif direction == "sell":
        sl_price = _recent_swing_high_5m(m_trigger)

    invalidation = (
        "If the most recent 5m/15m swing low (buy) or swing high (sell) is violated, treat setup as failed."
    )

    if direction == "buy":
        prob = _probability("buy", daily_bias, weekly_bias, buy_ok)
    elif direction == "sell":
        prob = _probability("sell", daily_bias, weekly_bias, sell_ok)
    elif direction == "mixed":
        prob = _probability("none", daily_bias, weekly_bias, True)
    else:
        prob = _probability("none", daily_bias, weekly_bias, False)

    swing_line = (
        f"Structural swing high ≈ {sh:.2f}, swing low ≈ {sl:.2f}"
        if sh is not None and sl is not None
        else "Swings not fully available"
    )
    if direction == "sell":
        trigger_note = sell_note
    elif direction == "buy":
        trigger_note = buy_note
    elif direction == "mixed":
        trigger_note = buy_note
    else:
        trigger_note = f"Buy: {buy_note} | Sell: {sell_note}"
    notes = [
        swing_line,
        trigger_note,
        "Confirmation: stronger when price breaks, moves away, then revisits the structural level before entry.",
    ]

    return {
        "label": label,
        "structural_swing_high": sh,
        "structural_swing_low": sl,
        "direction": direction,
        "entry_probability": prob,
        "entry": entry,
        "stop_loss": sl_price,
        "target": target,
        "invalidation": invalidation,
        "notes": notes,
    }


def analyze_pack(
    daily: list[Candle],
    h60: list[Candle],
    m30: list[Candle],
    m15: list[Candle],
    m5: list[Candle],
) -> dict[str, Any]:
    d_bias = daily_trend_bias(daily)
    w_bias = weekly_bias_from_daily(daily)

    trend_block = {
        "one_week_and_one_day": {
            "weekly_bias": w_bias,
            "daily_bias": d_bias,
            "summary": f"Weekly≈{w_bias}, daily≈{d_bias} (SMA20-style daily filter).",
        }
    }

    intra = build_section("Intraday (30m + 5m)", m30, m5, d_bias, w_bias)
    intra["timeframes"] = "30m structure / 5m trigger"

    pos = build_section("Positional ~2 weeks (60m + 15m)", h60, m15, d_bias, w_bias)
    pos["timeframes"] = "60m structure / 15m trigger"

    return {
        "trend": trend_block,
        "intraday": intra,
        "positional": pos,
    }
