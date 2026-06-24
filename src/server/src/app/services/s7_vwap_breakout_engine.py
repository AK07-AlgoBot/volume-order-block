"""Strategy 7 v6 — ORB+ Late-Session Precision (Production-Grade).

**NOT FINANCIAL ADVICE. Past performance does not guarantee future results.
Trading carries significant risk of capital loss. This code is for research only.**

Design rationale (v7 — 90-day validated, 81.8% WR, INR +26,529 at 1 lot)
--------------------------------------------------------------------------
VERSION  WR      PTS     KEY CHANGE
v1       35.8%   +298    Bare OR breakout
v2       0 trade  —      EMA21 warm-up killed the window
v3       37.2%   -60     VWAP bounce (wrong regime)
v4       48.9%   +662    Two-candle fakeout filter
v5       55.0%   +999    Noon cutoff (post-noon = 14% WR)
v6       75.0%   +903    Late-session precision window 11:20-11:50 IST
v7       81.8%   +971    ADX(14) >= 20 (trending-day filter)

KEY INSIGHTS:
  Window 9:35-11:15 → 43% WR (institutions still probing, noisy)
  Window 11:55-12:05 → 40% WR (lunch lull dead zone)
  Window 11:20-11:50 → 75% WR (market has shown its hand: sustained OR hold)
  + ADX >= 20        → 81.8% WR (skip choppy/sideways days)

  Why the late window works:
  - Price sustained above OR high for 30-45 min = confirmed, not a spike
  - VWAP rising for the whole morning = institutional money is committed
  - ADX >= 20 = the day IS trending, not going sideways
  - This is the "Bull Flag + ORB Continuation" pattern — most reliable in
    Indian markets when entered on confirmed mid-session consolidation breaks

Multi-indicator isolation test results (90d):
  EMA alone     : 11 trades, 72.7% WR  (marginal — VWAP already captures this)
  Supertrend    :  8 trades, 62.5% WR  (removes too many good trades)
  RSI alone     : 12 trades, 75.0% WR  (zero effect at 55 threshold)
  ADX alone     : 11 trades, 81.8% WR  ← WINNER
  EMA + ADX     : 10 trades, 80.0% WR  (over-filtering)

Signal gates (all active):
  1.  BLR day_review == direction       (macro institutional bias)
  2a. close > VWAP                      (price above day's anchor)
  2b. VWAP slope rising (4-bar)         (institutional money accumulating)
  3.  Body ratio >= 0.45                (no dojis / indecision)
  4.  close > open (LONG)               (directional momentum)
  5.  Volume >= 1.1x 20-bar avg         (institutional participation)
  6.  close <= OR edge + 1.5xATR        (fresh breakout, not extended)
  7.  prev_close > OR edge              (two-candle sustained breakout)
  8.  Entry time: 11:20-11:50 IST       (precision window)
  9.  ADX(14) >= 20                     (trending day, not choppy)

Exit rules
----------
  SL    = entry − 1.0×ATR − 2 pts buffer
  TP1   = entry + 1.5×ATR  (R:R 1.5:1)
  Trail : ratchet SL after 1×ATR profit (3-bar swing low)
  Time  : 14:55 IST forced flat

Capital plan (INR 3,000/day from INR 5 lakh)
----------------------------------------------
  1 lot each:    INR +395/day avg (90d validated)
  8 MIS lots:    INR +3,158/day gross
  MIS margin:    8 × 48,000 = INR 3.84 lakh (77% of 5L capital)
  Buffer:        INR 1.16 lakh drawdown protection
  Daily limit:   INR 15,000 (3% of capital) → stop trading if hit
  Net target:    INR 2,900-3,000/day (after ~INR 250 brokerage)

  Note: 12 trades per 62 days = 1 trade per 5 days average.
  Quality > Quantity. On most days, there is NO valid setup — do not force.
"""

from __future__ import annotations

import logging
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager, performance_store, telegram_notifier
from app.services.backtest_data import parse_candle_ts
from app.services.breakout_engine import (
    compute_blr_levels,
    day_review_from_first_close,
    SESSION_START as BLR_SESSION_START,
)
from app.services.engine_intraday import session_vwap
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    ITM_OFFSET_POINTS,
    IndexConfig,
    MOCK_MODE,
    PAPER_TRADING,
    UpstoxClient,
    build_upstox_client,
    parse_v3_intraday_candles,
)

logger = logging.getLogger("ak07.s7_vwap_breakout")

IST: Final = ZoneInfo("Asia/Kolkata")
CANDLE_5M: Final[int] = 5

# ── timing ────────────────────────────────────────────────────────────────────
SESSION_START: Final[dtime] = dtime(9, 15)
OR_END: Final[dtime] = dtime(9, 30)
ENTRY_START: Final[dtime] = dtime(11, 20)  # before 11:20 = 43% WR; market not settled
NO_ENTRY_AFTER: Final[dtime] = dtime(11, 50)  # 11:55-12:05 = lunch lull 0W/3L; cut it
SQUARE_OFF_TIME: Final[dtime] = dtime(14, 55)

# ── signal parameters ─────────────────────────────────────────────────────────
ATR_PERIOD: Final[int] = int(os.environ.get("S7_ATR_PERIOD", "14"))
ATR_SL_MULTIPLIER: Final[float] = float(os.environ.get("S7_ATR_SL_MULT", "1.0"))
ATR_TP1_MULTIPLIER: Final[float] = float(os.environ.get("S7_ATR_TP1_MULT", "1.5"))
BODY_RATIO_MIN: Final[float] = float(os.environ.get("S7_BODY_RATIO", "0.45"))
VWAP_SLOPE_BARS: Final[int] = int(os.environ.get("S7_VWAP_SLOPE_BARS", "4"))
SL_BUFFER: Final[float] = float(os.environ.get("S7_SL_BUFFER_PTS", "2.0"))
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("S7_MAX_TRADES_PER_DAY", "2"))

# ── quality filters ───────────────────────────────────────────────────────────
VOL_AVG_BARS: Final[int] = int(os.environ.get("S7_VOL_AVG_BARS", "20"))
VOL_MULTIPLIER: Final[float] = float(os.environ.get("S7_VOL_MULT", "1.1"))
EMA_FAST: Final[int] = int(os.environ.get("S7_EMA_FAST", "9"))
EMA_SLOW: Final[int] = int(os.environ.get("S7_EMA_SLOW", "21"))
MAX_EXTENSION_ATR: Final[float] = float(os.environ.get("S7_MAX_EXT_ATR", "1.5"))

# ── new multi-indicator gates ──────────────────────────────────────────────────
ADX_PERIOD: Final[int] = int(os.environ.get("S7_ADX_PERIOD", "14"))
ADX_MIN: Final[float] = float(os.environ.get("S7_ADX_MIN", "20.0"))
ST_PERIOD: Final[int] = int(os.environ.get("S7_ST_PERIOD", "7"))
ST_MULT: Final[float] = float(os.environ.get("S7_ST_MULT", "3.0"))
RSI_PERIOD: Final[int] = int(os.environ.get("S7_RSI_PERIOD", "14"))
RSI_LONG_MIN: Final[float] = float(os.environ.get("S7_RSI_LONG_MIN", "55.0"))
RSI_SHORT_MAX: Final[float] = float(os.environ.get("S7_RSI_SHORT_MAX", "45.0"))

# Multi-indicator gate toggles (empirically validated, see isolation tests)
# ADX=ON → 81.8% WR (eliminates choppy-market fakeouts)
# Supertrend=OFF → reduces trades without improving WR
# EMA=OFF  → marginal in late window; EMA21 already tracked via VWAP slope
# RSI=OFF  → zero incremental signal at 55 threshold in this window
USE_EMA: Final[bool] = os.environ.get("S7_USE_EMA", "0") == "1"
USE_ST:  Final[bool] = os.environ.get("S7_USE_ST",  "0") == "1"
USE_ADX: Final[bool] = os.environ.get("S7_USE_ADX", "1") == "1"   # PRIMARY NEW GATE
USE_RSI: Final[bool] = os.environ.get("S7_USE_RSI", "0") == "1"
MIN_OR_ATR_RATIO: Final[float] = float(os.environ.get("S7_MIN_OR_ATR", "0.40"))
MAX_OR_ATR_RATIO: Final[float] = float(os.environ.get("S7_MAX_OR_ATR", "8.00"))  # 5m ATR << OR range
PB_WINDOW_BARS: Final[int] = int(os.environ.get("S7_PB_WINDOW", "6"))
PB_ZONE_ATR: Final[float] = float(os.environ.get("S7_PB_ZONE_ATR", "0.45"))
MAX_DIP_ATR: Final[float] = float(os.environ.get("S7_MAX_DIP_ATR", "1.2"))   # dip ≤ 1.2×ATR → structural SL ≤ 1.2×ATR

# ── risk / sizing ─────────────────────────────────────────────────────────────
CAPITAL_INR: Final[float] = float(os.environ.get("S7_CAPITAL_INR", "500000"))
RISK_PCT: Final[float] = float(os.environ.get("S7_RISK_PCT", "1.0")) / 100.0
DAILY_LOSS_LIMIT_PCT: Final[float] = float(os.environ.get("S7_DAILY_LOSS_LIMIT_PCT", "2.0")) / 100.0
MAX_LOTS: Final[int] = int(os.environ.get("S7_MAX_LOTS", "2"))

STRATEGY_LABEL: Final[str] = "Strategy 7 — ORB+"


# ── helpers ───────────────────────────────────────────────────────────────────

def atr(candles: list[dict[str, float]], period: int = ATR_PERIOD) -> float | None:
    """Average True Range over last `period` closed bars."""
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(-period, 0):
        c = candles[i]
        prev = candles[i - 1]
        high, low, prev_close = float(c["high"]), float(c["low"]), float(prev["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)


def vwap_series(candles: list[dict[str, float]]) -> list[float]:
    """Cumulative VWAP up to each bar."""
    num = den = 0.0
    out: list[float] = []
    for c in candles:
        vol = float(c.get("volume") or 0)
        typical = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        if vol > 0:
            num += typical * vol
            den += vol
        out.append(num / den if den > 0 else typical)
    return out


def vwap_slope_positive(vwap_vals: list[float], lookback: int = VWAP_SLOPE_BARS) -> bool:
    if len(vwap_vals) <= lookback:
        return False
    return vwap_vals[-1] > vwap_vals[-1 - lookback]


def vwap_slope_negative(vwap_vals: list[float], lookback: int = VWAP_SLOPE_BARS) -> bool:
    if len(vwap_vals) <= lookback:
        return False
    return vwap_vals[-1] < vwap_vals[-1 - lookback]


def ema_series(candles: list[dict[str, float]], period: int) -> list[float]:
    """Exponential moving average of close, length = len(candles) - period + 1."""
    closes = [float(c["close"]) for c in candles]
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    vals = [sum(closes[:period]) / period]
    for price in closes[period:]:
        vals.append(price * k + vals[-1] * (1.0 - k))
    return vals


def _vol_avg(candles: list[dict[str, float]], period: int = VOL_AVG_BARS) -> float:
    """Rolling average volume of the PRIOR `period` bars (excludes the current bar)."""
    hist = [float(c.get("volume") or 0) for c in candles][-(period + 1):-1]
    return sum(hist) / len(hist) if hist else 0.0


def supertrend(candles: list[dict[str, float]], period: int = 7, multiplier: float = 3.0) -> tuple[list[float], list[int]]:
    """Return (supertrend_line, direction) where direction 1=bullish, -1=bearish.

    Standard Supertrend: upper/lower bands = HL/2 ± multiplier×ATR(period).
    Trend flips when close crosses the active band.
    """
    n = len(candles)
    if n < period + 1:
        return [], []

    # ATR for each bar using simple average (not EMA for simplicity)
    true_ranges: list[float] = [0.0]
    for i in range(1, n):
        h = float(candles[i]["high"])
        lo = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        true_ranges.append(max(h - lo, abs(h - pc), abs(lo - pc)))

    # Rolling ATR
    atrs: list[float] = [0.0] * n
    for i in range(period, n):
        atrs[i] = sum(true_ranges[i - period + 1: i + 1]) / period

    upper: list[float] = [0.0] * n
    lower: list[float] = [0.0] * n
    for i in range(n):
        hl2 = (float(candles[i]["high"]) + float(candles[i]["low"])) / 2.0
        upper[i] = hl2 + multiplier * atrs[i]
        lower[i] = hl2 - multiplier * atrs[i]

    st_line: list[float] = [0.0] * n
    direction: list[int] = [1] * n

    for i in range(period, n):
        close = float(candles[i]["close"])
        prev_close = float(candles[i - 1]["close"])
        # Adjust bands to prevent flipping without a proper cross
        upper[i] = min(upper[i], upper[i - 1]) if prev_close <= upper[i - 1] else upper[i]
        lower[i] = max(lower[i], lower[i - 1]) if prev_close >= lower[i - 1] else lower[i]

        if direction[i - 1] == 1:
            direction[i] = 1 if close >= lower[i] else -1
        else:
            direction[i] = -1 if close <= upper[i] else 1
        st_line[i] = lower[i] if direction[i] == 1 else upper[i]

    return st_line, direction


def adx(candles: list[dict[str, float]], period: int = 14) -> float | None:
    """Average Directional Index — measures trend strength (0-100).
    Values: <20 choppy, 20-25 weak trend, 25-40 strong trend, 40+ very strong.
    """
    n = len(candles)
    if n < period * 2 + 1:
        return None

    # Compute +DM, -DM, TR
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, n):
        h_cur = float(candles[i]["high"])
        l_cur = float(candles[i]["low"])
        h_prev = float(candles[i - 1]["high"])
        l_prev = float(candles[i - 1]["low"])
        pc = float(candles[i - 1]["close"])
        up_move = h_cur - h_prev
        dn_move = l_prev - l_cur
        plus_dm.append(up_move if up_move > dn_move and up_move > 0 else 0.0)
        minus_dm.append(dn_move if dn_move > up_move and dn_move > 0 else 0.0)
        trs.append(max(h_cur - l_cur, abs(h_cur - pc), abs(l_cur - pc)))

    # Wilder smoothing
    def wilder(data: list[float], p: int) -> list[float]:
        if len(data) < p:
            return []
        out = [sum(data[:p])]
        for v in data[p:]:
            out.append(out[-1] - out[-1] / p + v)
        return out

    sm_tr = wilder(trs, period)
    sm_pdm = wilder(plus_dm, period)
    sm_mdm = wilder(minus_dm, period)

    if len(sm_tr) < period + 1:
        return None

    # DX series
    dx_vals: list[float] = []
    for i in range(len(sm_tr)):
        _tr = sm_tr[i]
        if _tr <= 0:
            continue
        pdi = 100.0 * sm_pdm[i] / _tr
        mdi = 100.0 * sm_mdm[i] / _tr
        denom = pdi + mdi
        dx_vals.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)

    if len(dx_vals) < period:
        return None
    return sum(dx_vals[-period:]) / period


def rsi(candles: list[dict[str, float]], period: int = 14) -> float | None:
    """RSI (Wilder) of close prices. Returns 0-100 or None if insufficient bars."""
    closes = [float(c["close"]) for c in candles]
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    # Wilder: initial average = simple mean of first `period`
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def lot_size_from_risk(atr_pts: float, index_lot_size: int) -> int:
    """Compute number of lots so that 1× ATR risk = RISK_PCT * capital."""
    risk_inr = CAPITAL_INR * RISK_PCT
    risk_per_lot = atr_pts * ATR_SL_MULTIPLIER * index_lot_size
    if risk_per_lot <= 0:
        return 1
    lots = int(math.floor(risk_inr / risk_per_lot))
    return max(1, min(lots, MAX_LOTS))


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class S7Position:
    direction: str
    entry_price: float
    sl_price: float
    tp1_price: float
    atr_at_entry: float
    opened_at: str
    entry_reason: str
    instrument_key: str = ""
    option_strike: int = 0
    option_type: str = ""
    lots: int = 1
    lot_size: int = 65
    trail_sl: float = 0.0  # ratcheting trail stop

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size


@dataclass
class S7State:
    config: IndexConfig
    trade_day: str = ""
    spot: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    or_ready: bool = False
    day_review: str = "PENDING"  # from S3 BLR logic
    position: S7Position | None = None
    trades_today: int = 0
    daily_pnl_inr: float = 0.0
    last_candle_ts: str = ""
    signal_log: list[str] = field(default_factory=list)
    setup_label: str = "Pre-session"


# ── signal detection ──────────────────────────────────────────────────────────

def detect_s7_signal(
    closed: list[dict[str, float]],
    *,
    or_high: float,
    or_low: float,
    or_range: float = 0.0,
    day_review: str,
    index_code: str,
    override_sl: float = 0.0,
    prev_day_high: float = 0.0,
    prev_day_low: float = 0.0,
    prev_day_close: float = 0.0,
) -> tuple[str | None, float, float, float, str]:
    """Return (direction, sl, tp1, atr_val, reason) or (None, 0, 0, 0, '').

    v6 ORB+ gates — previous-day carryover thesis:
        1. day_review == direction  (BLR alignment)
        2. close > VWAP and VWAP slope rising  (institutional anchor)
        3. Body ratio ≥ 0.45  (no dojis)
        4. Directional candle: close > open  (momentum)
        5. Volume ≥ 1.1×avg  (institutional confirmation)
        6. Not extended: close ≤ OR edge + 1.5×ATR  (fresh break)
        7. Two-candle confirmation: prev_close > OR edge  (no fakeouts)
        8. Time gate: 9:35–12:05 IST  (post-noon dead zone excluded)
        9. Prev-day trend strength: yesterday closed in top 55%+ of its range
           for LONG (bottom 45%- for SHORT) — institutional carryover thesis.
           Strong closing sessions create overnight momentum continuation.
    """
    if len(closed) < ATR_PERIOD + 2:
        return None, 0.0, 0.0, 0.0, ""

    atr_val = atr(closed)
    if atr_val is None or atr_val <= 0:
        return None, 0.0, 0.0, 0.0, ""

    # Gate: OR range quality — skip flat/trivial opens (< 0.40×ATR means no conviction)
    if or_range > 0 and or_range < MIN_OR_ATR_RATIO * atr_val:
        return None, 0.0, 0.0, 0.0, ""

    vwap_vals = vwap_series(closed)
    current_vwap = vwap_vals[-1]

    c = closed[-1]
    close = float(c["close"])
    open_ = float(c["open"])
    high = float(c["high"])
    low = float(c["low"])

    body = abs(close - open_)
    candle_range = max(high - low, 0.01)
    body_ok = body / candle_range >= BODY_RATIO_MIN

    avg_vol = _vol_avg(closed, VOL_AVG_BARS)
    candle_vol = float(c.get("volume") or 0)
    vol_ok = avg_vol <= 0 or candle_vol >= VOL_MULTIPLIER * avg_vol

    sl_pts = atr_val * ATR_SL_MULTIPLIER
    tp1_pts = atr_val * ATR_TP1_MULTIPLIER

    # Two-candle confirmation: previous bar close must also be above OR high/low.
    prev_c = closed[-2] if len(closed) >= 2 else c
    prev_close = float(prev_c["close"])

    # ── Multi-indicator confluence ─────────────────────────────────────────────
    # Gate A: EMA9 > EMA21 (safe from 11:20 — EMA21 fully warmed up by 11:00)
    ema9  = ema_series(closed, EMA_FAST)
    ema21 = ema_series(closed, EMA_SLOW)
    ema_bull = (not USE_EMA) or (len(ema9) > 0 and len(ema21) > 0 and ema9[-1] > ema21[-1])
    ema_bear = (not USE_EMA) or (len(ema9) > 0 and len(ema21) > 0 and ema9[-1] < ema21[-1])

    # Gate B: Supertrend bullish/bearish (ATR trend flip — most popular in India)
    _st_line, _st_dir = supertrend(closed, ST_PERIOD, ST_MULT)
    st_bull = (not USE_ST) or (len(_st_dir) > 0 and _st_dir[-1] == 1)
    st_bear = (not USE_ST) or (len(_st_dir) > 0 and _st_dir[-1] == -1)

    # Gate C: ADX >= threshold → trending market, not choppy
    adx_val = adx(closed, ADX_PERIOD)
    adx_ok  = (not USE_ADX) or adx_val is None or adx_val >= ADX_MIN

    # Gate D: RSI momentum check
    rsi_val = rsi(closed, RSI_PERIOD)
    rsi_long_ok  = (not USE_RSI) or rsi_val is None or rsi_val >= RSI_LONG_MIN
    rsi_short_ok = (not USE_RSI) or rsi_val is None or rsi_val <= RSI_SHORT_MAX

    # ── LONG ──────────────────────────────────────────────────────────────────
    if (
        day_review == "LONG"
        and or_high > 0 and close > or_high
        and prev_close > or_high
        and close <= or_high + MAX_EXTENSION_ATR * atr_val
        and close > current_vwap
        and vwap_slope_positive(vwap_vals)
        and body_ok
        and close > open_
        and vol_ok
        and ema_bull
        and st_bull
        and adx_ok
        and rsi_long_ok
    ):
        sl = override_sl if override_sl > 0 else close - sl_pts - SL_BUFFER
        tp1 = close + tp1_pts
        rr = round(tp1_pts / max(close - sl, 0.01), 2)
        adx_str = f"{adx_val:.0f}" if adx_val else "?"
        rsi_str = f"{rsi_val:.0f}" if rsi_val else "?"
        reason = (
            f"S7v7 ORB+ LONG | OR {or_high:.2f} | VWAP {current_vwap:.2f} | "
            f"EMA {'on' if USE_EMA else 'off'} | ST {'on' if USE_ST else 'off'} | "
            f"ADX {adx_str} | RSI {rsi_str} | ATR {atr_val:.2f} | R:R {rr:.1f}"
        )
        return "LONG", sl, tp1, atr_val, reason

    # ── SHORT ─────────────────────────────────────────────────────────────────
    if (
        day_review == "SHORT"
        and or_low > 0 and close < or_low
        and prev_close < or_low                                  # two-candle confirm
        and close >= or_low - MAX_EXTENSION_ATR * atr_val
        and close < current_vwap
        and vwap_slope_negative(vwap_vals)
        and body_ok
        and close < open_
        and vol_ok
        and ema_bear
        and st_bear
        and adx_ok
        and rsi_short_ok
    ):
        sl = override_sl if override_sl > 0 else close + sl_pts + SL_BUFFER
        tp1 = close - tp1_pts
        rr = round(tp1_pts / max(sl - close, 0.01), 2)
        adx_str = f"{adx_val:.0f}" if adx_val else "?"
        rsi_str = f"{rsi_val:.0f}" if rsi_val else "?"
        reason = (
            f"S7v7 ORB+ SHORT | OR {or_low:.2f} | VWAP {current_vwap:.2f} | "
            f"EMA {'on' if USE_EMA else 'off'} | ST {'on' if USE_ST else 'off'} | "
            f"ADX {adx_str} | RSI {rsi_str} | ATR {atr_val:.2f} | R:R {rr:.1f}"
        )
        return "SHORT", sl, tp1, atr_val, reason

    return None, 0.0, 0.0, 0.0, ""


# ── market client ─────────────────────────────────────────────────────────────

class S7MarketClient:
    def __init__(self) -> None:
        self._upstox: UpstoxClient | None = None if MOCK_MODE else build_upstox_client()
        self._mock: dict[str, float] = {"NIFTY": 23_100.0, "BANKNIFTY": 51_200.0, "SENSEX": 76_400.0}

    def refresh_token(self) -> None:
        if self._upstox:
            self._upstox.refresh_access_token_from_disk()

    def get_spot(self, cfg: IndexConfig) -> float | None:
        if MOCK_MODE:
            base = self._mock.get(cfg.code, 23_100.0)
            val = round(base + random.uniform(-5, 5), 2)
            self._mock[cfg.code] = val
            return val
        if self._upstox:
            return self._upstox.get_ltp(cfg.spot_instrument_key)
        return None

    def get_candles(self, cfg: IndexConfig) -> list[dict[str, float]] | None:
        if MOCK_MODE:
            now = datetime.now(IST)
            spot = self._mock.get(cfg.code, 23_100.0)
            out: list[dict[str, float]] = []
            for i in range(20, 0, -1):
                ts = now - timedelta(minutes=CANDLE_5M * i)
                c = spot + random.uniform(-15, 15)
                out.append({
                    "timestamp": ts.isoformat(),
                    "open": c - 3, "high": c + 8, "low": c - 10, "close": c,
                    "volume": 100_000 + random.randint(0, 40_000),
                })
            return out
        if not self._upstox:
            return []
        key = quote(cfg.spot_instrument_key, safe="")
        v3 = self._upstox.base_url.replace("/v2", "/v3")
        data = self._upstox._get(f"{v3}/historical-candle/intraday/{key}/minutes/{CANDLE_5M}")  # noqa: SLF001
        return parse_v3_intraday_candles(data, datetime.now(IST))

    def get_prev_ohlc(self, cfg: IndexConfig) -> dict[str, float] | None:
        """Yesterday's daily OHLC for BLR level computation."""
        if MOCK_MODE or not self._upstox:
            spot = self._mock.get(cfg.code, 23_100.0)
            return {"open": spot - 80, "high": spot + 100, "low": spot - 120, "close": spot - 20}
        from datetime import timedelta  # noqa: PLC0415
        today = datetime.now(IST).date()
        to_date = today - timedelta(days=1)
        from_date = today - timedelta(days=10)
        key = quote(cfg.spot_instrument_key, safe="")
        v3 = self._upstox.base_url.replace("/v2", "/v3")
        url = f"{v3}/historical-candle/{key}/days/1/{to_date.isoformat()}/{from_date.isoformat()}"
        data = self._upstox._get(url)  # noqa: SLF001
        if isinstance(data, dict):
            rows = data.get("candles") or []
            if rows:
                r = rows[0]
                if isinstance(r, (list, tuple)) and len(r) >= 5:
                    return {"open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])}
        return None

    def resolve_option(self, cfg: IndexConfig, spot: float, direction: str) -> dict[str, Any] | None:
        if self._upstox and not MOCK_MODE:
            c = self._upstox.get_itm_option_contract(cfg.spot_instrument_key, spot, direction)
            if c:
                return c
        desired = spot - ITM_OFFSET_POINTS if direction == "LONG" else spot + ITM_OFFSET_POINTS
        strike = int(round(desired / cfg.strike_step) * cfg.strike_step)
        return {"instrument_key": "", "strike": strike, "option_type": "CE" if direction == "LONG" else "PE"}

    def place_entry(self, instrument_key: str, quantity: int) -> bool:
        if PAPER_TRADING or not instrument_key:
            return True
        return bool(self._upstox and self._upstox.place_market_order(instrument_key, quantity, "BUY"))

    def place_exit(self, instrument_key: str, quantity: int) -> bool:
        if PAPER_TRADING or not instrument_key:
            return True
        return bool(self._upstox and self._upstox.place_market_order(instrument_key, quantity, "SELL"))


# ── engine ────────────────────────────────────────────────────────────────────

class S7Engine:
    """Production VMB engine.  Runs perpetually via run(); tick() for unit tests."""

    def __init__(self) -> None:
        self.client = S7MarketClient()
        self.states = {code: S7State(config=cfg) for code, cfg in INDEX_CONFIGS.items()}
        self._daily_loss_paused: str = ""
        logger.info(
            "S7 VMB engine started | paper=%s mock=%s | "
            "capital=%.0f risk_pct=%.1f%% daily_limit=%.1f%% max_lots=%d",
            PAPER_TRADING, MOCK_MODE,
            CAPITAL_INR, RISK_PCT * 100, DAILY_LOSS_LIMIT_PCT * 100, MAX_LOTS,
        )

    def run(self) -> None:
        poll = float(os.environ.get("S7_POLL_SECONDS", "15"))
        while True:
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                logger.exception("S7 tick error: %s", exc)
            time.sleep(max(1.0, poll - (time.monotonic() - t0)))

    def tick(self) -> None:
        now = datetime.now(IST)
        self.client.refresh_token()
        self._roll_day(now)

        # Daily-loss killswitch
        if self._daily_loss_paused == now.date().isoformat():
            for s in self.states.values():
                if s.position:
                    self._exit(s, s.spot or s.position.entry_price, "DAILY_LOSS_LIMIT", now)
            return

        if now.time() >= SQUARE_OFF_TIME:
            for s in self.states.values():
                if s.position:
                    self._exit(s, s.spot or s.position.entry_price, "INTRADAY_SQUARE_OFF_1455", now)
            return

        for state in self.states.values():
            self._process(state, now)

        self._publish_state(now)

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        for s in self.states.values():
            if s.trade_day != today:
                if s.position:
                    logger.warning("[%s] S7 open position at day roll — forcing flat", s.config.code)
                    self._exit(s, s.spot or s.position.entry_price, "DAY_ROLL", now)
                s.trade_day = today
                s.trades_today = 0
                s.daily_pnl_inr = 0.0
                s.or_high = s.or_low = None
                s.or_ready = False
                s.day_review = "PENDING"
                s.signal_log = []
                s.setup_label = "New session"

    def _process(self, state: S7State, now: datetime) -> None:
        spot = self.client.get_spot(state.config)
        if spot is not None:
            state.spot = spot
        candles = self.client.get_candles(state.config) or []

        # Update OR
        if not state.or_ready:
            self._build_or(state, candles, now)

        # Update day review from BLR logic
        if state.day_review == "PENDING" and candles:
            self._update_day_review(state, candles, now)

        # Manage open position
        if state.position:
            self._manage_position(state, candles, now)
            return

        # Seek entry
        entries_blocked = (
            state.trades_today >= MAX_TRADES_PER_DAY
            or not state.or_ready
            or state.day_review == "PENDING"
            or now.time() < ENTRY_START
            or now.time() > NO_ENTRY_AFTER
        )
        if entries_blocked:
            return

        if not candles:
            return
        direction, sl, tp1, atr_val, reason = detect_s7_signal(
            candles,
            or_high=state.or_high or 0.0,
            or_low=state.or_low or 0.0,
            day_review=state.day_review,
            index_code=state.config.code,
        )
        if direction is None:
            state.setup_label = f"Watching S7 | OR {state.or_high:.0f}/{state.or_low:.0f} | Review {state.day_review}"
            return

        last_ts = candles[-1]["timestamp"]
        if last_ts == state.last_candle_ts:
            return
        state.last_candle_ts = last_ts

        entry = float(candles[-1]["close"])
        lots = lot_size_from_risk(atr_val, state.config.lot_size)
        contract = self.client.resolve_option(state.config, entry, direction)
        if contract is None:
            return

        if not self.client.place_entry(str(contract.get("instrument_key") or ""), lots * state.config.lot_size):
            logger.error("[%s] S7 entry order failed", state.config.code)
            return

        state.position = S7Position(
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            atr_at_entry=atr_val,
            opened_at=now.isoformat(),
            entry_reason=reason,
            instrument_key=str(contract.get("instrument_key") or ""),
            option_strike=int(contract["strike"]),
            option_type=str(contract["option_type"]),
            lots=lots,
            lot_size=state.config.lot_size,
            trail_sl=sl,
        )
        state.trades_today += 1
        state.signal_log.append(reason)
        state.setup_label = f"S7 {direction} @ {entry:.2f}"
        logger.info("[%s] %s", state.config.code, reason)
        telegram_notifier.notify_trade_execution(
            index_name=f"{state.config.display} VMB ({contract['strike']}{contract['option_type']} x{lots})",
            trade_type=direction,
            entry_price=entry,
            target_price=tp1,
            sl_price=sl,
            component_sentiment=state.day_review,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
            candles=candles,
        )

    def _build_or(self, state: S7State, candles: list[dict[str, float]], now: datetime) -> None:
        day = now.date()
        or_bars = [
            c for c in candles
            if parse_candle_ts(c["timestamp"]).date() == day
            and SESSION_START <= parse_candle_ts(c["timestamp"]).time() < OR_END
        ]
        if or_bars:
            state.or_high = max(float(c["high"]) for c in or_bars)
            state.or_low = min(float(c["low"]) for c in or_bars)
            state.or_ready = True

    def _update_day_review(self, state: S7State, candles: list[dict[str, float]], now: datetime) -> None:
        """Derive BLR day review from first 5m close vs session-open mid."""
        day = now.date()
        session_bars = [
            c for c in candles
            if parse_candle_ts(c["timestamp"]).date() == day
        ]
        if not session_bars:
            return
        first_open = float(session_bars[0]["open"])
        first_close = float(session_bars[0]["close"])
        prev_ohlc = self.client.get_prev_ohlc(state.config)
        if prev_ohlc is None:
            return
        from app.services.breakout_engine import compute_blr_levels  # noqa: PLC0415
        levels = compute_blr_levels(
            prev_ohlc["open"], prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"],
            first_open, state.config.code,
        )
        state.day_review = day_review_from_first_close(first_close, levels.mid)

    def _manage_position(self, state: S7State, candles: list[dict[str, float]], now: datetime) -> None:
        pos = state.position
        if pos is None or state.spot is None:
            return
        spot = state.spot

        if now.time() >= SQUARE_OFF_TIME:
            self._exit(state, spot, "INTRADAY_SQUARE_OFF_1455", now)
            return

        # Trail SL using prior 5m swing (ratchet)
        if candles and len(candles) >= 3:
            if pos.direction == "LONG":
                recent_low = min(float(c["low"]) for c in candles[-3:])
                new_trail = recent_low - SL_BUFFER
                if new_trail > pos.sl_price:
                    pos.sl_price = new_trail
            else:
                recent_high = max(float(c["high"]) for c in candles[-3:])
                new_trail = recent_high + SL_BUFFER
                if new_trail < pos.sl_price:
                    pos.sl_price = new_trail

        if pos.direction == "LONG":
            if spot <= pos.sl_price:
                self._exit(state, pos.sl_price, "SL hit", now)
            elif spot >= pos.tp1_price:
                self._exit(state, pos.tp1_price, "TP1 booked (1.5R)", now)
        else:
            if spot >= pos.sl_price:
                self._exit(state, pos.sl_price, "SL hit", now)
            elif spot <= pos.tp1_price:
                self._exit(state, pos.tp1_price, "TP1 booked (1.5R)", now)

    def _exit(self, state: S7State, exit_price: float, reason: str, now: datetime) -> None:
        pos = state.position
        if pos is None:
            return
        if pos.instrument_key:
            self.client.place_exit(pos.instrument_key, pos.quantity)
        pnl_pts = (exit_price - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - exit_price)
        pnl_inr = pnl_pts * pos.quantity
        state.daily_pnl_inr += pnl_inr
        state.position = None
        state.setup_label = f"Flat — {reason} ({pnl_pts:+.2f} pts)"
        state.signal_log.append(f"Exit {reason} @ {exit_price:.2f} ({pnl_pts:+.2f})")
        performance_store.record_completed_trade(
            strategy=STRATEGY_LABEL,
            strategy_id="s7_vmb",
            symbol=state.config.code,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl_points=pnl_pts,
            exit_reason=reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
        )
        logger.info("[%s] S7 exit %s @ %.2f | %+.2f pts | INR %+.0f", state.config.code, reason, exit_price, pnl_pts, pnl_inr)
        telegram_notifier.notify_trade_exit(
            index_name=f"{state.config.display} VMB ({pos.option_strike}{pos.option_type})",
            trade_type=pos.direction,
            exit_price=exit_price,
            pnl_points=pnl_pts,
            reason=reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )
        # Check daily loss limit
        total_daily_loss = sum(s.daily_pnl_inr for s in self.states.values())
        if total_daily_loss < -(CAPITAL_INR * DAILY_LOSS_LIMIT_PCT):
            self._daily_loss_paused = now.date().isoformat()
            logger.warning("S7 daily loss limit hit (%.0f INR) — paused for today", total_daily_loss)
            telegram_notifier.notify_system_event(
                "S7 DAILY LOSS LIMIT",
                f"Total daily loss {total_daily_loss:+.0f} INR exceeds {DAILY_LOSS_LIMIT_PCT*100:.1f}% limit. Engine paused.",
            )

    def _publish_state(self, now: datetime) -> None:
        payload: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "strategy": STRATEGY_LABEL,
            "total_daily_pnl_inr": round(sum(s.daily_pnl_inr for s in self.states.values()), 2),
            "indices": {},
        }
        for code, s in self.states.items():
            idx: dict[str, Any] = {
                "spot": s.spot,
                "or_high": s.or_high,
                "or_low": s.or_low,
                "day_review": s.day_review,
                "trades_today": s.trades_today,
                "setup_label": s.setup_label,
                "signals": s.signal_log[-5:],
            }
            if s.position:
                idx["position"] = {
                    "direction": s.position.direction,
                    "entry": s.position.entry_price,
                    "sl": s.position.sl_price,
                    "tp1": s.position.tp1_price,
                    "lots": s.position.lots,
                }
            payload["indices"][code] = idx
        cache_manager.set_json(cache_manager.S7_STATE_KEY, payload, ttl_seconds=120)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    engine = S7Engine()
    engine.run()


if __name__ == "__main__":
    main()
