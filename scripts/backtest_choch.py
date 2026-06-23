#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════════
 CHOCH — Change Of Character  |  5-min Reversal  |  90-day Backtest
════════════════════════════════════════════════════════════════════

⚠️  DISCLAIMER: NOT financial advice. For research purposes only.
    Past performance does not guarantee future results.

Strategy Logic
──────────────
1. Swing detection  : identify confirmed swing highs (SH) and lows (SL)
                      on 5-min bars using a 3-bar lookback on each side.
2. Market structure : BULL when last two swings form HH + HL;
                      BEAR when last two swings form LL + LH.
3. CHOCH signal     : in BULL structure, 5-min close below last SL  → SHORT.
                      in BEAR structure, 5-min close above last SH  → LONG.
4. HTF filter       : 1H trend (EMA-20 on aggregated 1H bars built from 5-min).
                      Only LONG if price is above 1H EMA-20.
                      Only SHORT if price is below 1H EMA-20.
5. Volatility guard : 5-min ADX(14) > 20 (skip choppy / ranging markets).
6. Entry / risk     : Entry at close of CHOCH candle.
                      SL = 1.5 × ATR(14) beyond entry.
                      TP = 2 × risk distance (R:R = 1:2).
7. Time gates       : Entry only 09:30–14:00 IST.
                      Force square-off at 15:15 IST.
                      Max 2 trades per index per day.

Run
───
  cd volume-order-block
  $env:PYTHONPATH="src\\server\\src"
  python scripts/backtest_choch.py --days 90
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "server" / "src"))

from app.services.backtest_data import HistoricalDataClient, parse_candle_ts
from app.services.upstox_engine import INDEX_CONFIGS

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backtest_choch")

IST: Final = ZoneInfo("Asia/Kolkata")

# ── Strategy constants ─────────────────────────────────────────────────────────
SWING_LOOKBACK: Final[int] = 3          # bars each side for swing confirmation
ATR_PERIOD: Final[int] = 14
SL_ATR_MULT: Final[float] = 1.5        # SL = entry ± 1.5 × ATR
TP_RR: Final[float] = 2.0              # TP = entry ± (SL_dist × 2.0)
ADX_PERIOD: Final[int] = 14
ADX_MIN: Final[float] = 20.0           # skip ranging market
HTF_EMA_PERIOD: Final[int] = 20        # 1-hour EMA periods
MAX_TRADES_PER_DAY: Final[int] = 2
ENTRY_START: Final[dtime] = dtime(9, 30)
NO_ENTRY_AFTER: Final[dtime] = dtime(14, 0)
SQUARE_OFF: Final[dtime] = dtime(15, 15)
CAPITAL: Final[float] = 500_000.0      # ₹ 5 lakh
RISK_PCT: Final[float] = 0.01          # 1% risk per trade
LOT_MULTIPLIER: Final[float] = 65.0    # Nifty lot size (65 units)


# ── Indicators ────────────────────────────────────────────────────────────────

def _atr(candles: list[dict], period: int = ATR_PERIOD) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        c, p = candles[i], candles[i - 1]
        tr = max(
            float(c["high"]) - float(c["low"]),
            abs(float(c["high"]) - float(p["close"])),
            abs(float(c["low"]) - float(p["close"])),
        )
        trs.append(tr)
    return sum(trs) / len(trs)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _adx(candles: list[dict], period: int = ADX_PERIOD) -> float | None:
    if len(candles) < period * 2:
        return None
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(candles)):
        h, l, ph, pl, pc = (
            float(candles[i]["high"]), float(candles[i]["low"]),
            float(candles[i-1]["high"]), float(candles[i-1]["low"]),
            float(candles[i-1]["close"]),
        )
        up, down = h - ph, pl - l
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(tr_list) < period:
        return None
    def _smooth(lst):
        s = sum(lst[:period])
        res = [s]
        for v in lst[period:]:
            res.append(res[-1] - res[-1] / period + v)
        return res
    atr_s = _smooth(tr_list)
    pdm_s = _smooth(plus_dm)
    ndm_s = _smooth(minus_dm)
    if not atr_s or atr_s[-1] == 0:
        return None
    pdi = [100 * p / a for p, a in zip(pdm_s, atr_s)]
    ndi = [100 * n / a for n, a in zip(ndm_s, atr_s)]
    dx = [abs(p - n) / (p + n) * 100 if (p + n) > 0 else 0 for p, n in zip(pdi, ndi)]
    if len(dx) < period:
        return None
    adx_vals = _ema(dx, period)
    return adx_vals[-1]


# ── HTF (1H) builder ──────────────────────────────────────────────────────────

def build_1h_candles(candles_5m: list[dict]) -> list[dict]:
    """Aggregate 5-min candles into 1H candles (IST hour boundary)."""
    buckets: dict[str, dict] = {}
    for c in candles_5m:
        ts = parse_candle_ts(c["timestamp"])
        bucket_key = ts.replace(minute=0, second=0, microsecond=0).isoformat()
        if bucket_key not in buckets:
            buckets[bucket_key] = {
                "timestamp": bucket_key,
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume", 0)),
            }
        else:
            b = buckets[bucket_key]
            b["high"] = max(b["high"], float(c["high"]))
            b["low"] = min(b["low"], float(c["low"]))
            b["close"] = float(c["close"])
            b["volume"] = b["volume"] + float(c.get("volume", 0))
    return sorted(buckets.values(), key=lambda x: x["timestamp"])


def get_1h_ema_direction(candles_5m: list[dict], bar_ts: datetime) -> str | None:
    """
    Return 'BULL', 'BEAR', or None using 1H EMA-20.
    Uses only 1H candles whose close time is before bar_ts.
    """
    h1 = [c for c in build_1h_candles(candles_5m)
          if parse_candle_ts(c["timestamp"]) + timedelta(hours=1) < bar_ts]
    if len(h1) < HTF_EMA_PERIOD:
        return None
    closes = [float(c["close"]) for c in h1]
    ema = _ema(closes, HTF_EMA_PERIOD)
    if not ema:
        return None
    last_close = closes[-1]
    return "BULL" if last_close > ema[-1] else "BEAR"


# ── Swing + CHOCH detection ────────────────────────────────────────────────────

@dataclass
class StructureState:
    last_sh: float | None = None     # most recent confirmed swing high
    last_sl: float | None = None     # most recent confirmed swing low
    prev_sh: float | None = None
    prev_sl: float | None = None
    structure: str = "NEUTRAL"       # BULL | BEAR | NEUTRAL

    def reset(self) -> None:
        self.last_sh = self.last_sl = self.prev_sh = self.prev_sl = None
        self.structure = "NEUTRAL"


def update_structure(state: StructureState, closed: list[dict], lb: int = SWING_LOOKBACK) -> None:
    """
    Update swing state using closed bars.
    The bar confirmed at this tick is at position -(lb+1) (has lb bars to the right).
    """
    needed = 2 * lb + 1
    if len(closed) < needed:
        return
    idx = -(lb + 1)
    bar = closed[idx]
    h = float(bar["high"])
    lo = float(bar["low"])

    is_sh = (
        all(h > float(closed[idx - j]["high"]) for j in range(1, lb + 1)) and
        all(h >= float(closed[idx + j]["high"]) for j in range(1, lb + 1))
    )
    is_sl = (
        all(lo < float(closed[idx - j]["low"]) for j in range(1, lb + 1)) and
        all(lo <= float(closed[idx + j]["low"]) for j in range(1, lb + 1))
    )

    if is_sh:
        state.prev_sh = state.last_sh
        state.last_sh = h
    if is_sl:
        state.prev_sl = state.last_sl
        state.last_sl = lo

    # Determine structure from last two confirmed swings of each type
    if state.last_sh and state.prev_sh and state.last_sl and state.prev_sl:
        hh = state.last_sh > state.prev_sh
        hl = state.last_sl > state.prev_sl
        ll = state.last_sl < state.prev_sl
        lh = state.last_sh < state.prev_sh
        if hh and hl:
            state.structure = "BULL"
        elif ll and lh:
            state.structure = "BEAR"


def detect_choch(state: StructureState, closed: list[dict]) -> tuple[str | None, float]:
    """Return (direction, choch_level) or (None, 0) on the latest bar."""
    if not closed:
        return None, 0.0
    close = float(closed[-1]["close"])
    if state.structure == "BULL" and state.last_sl is not None:
        if close < state.last_sl:
            return "SHORT", state.last_sl
    elif state.structure == "BEAR" and state.last_sh is not None:
        if close > state.last_sh:
            return "LONG", state.last_sh
    return None, 0.0


# ── Position simulation ───────────────────────────────────────────────────────

@dataclass
class CHOCHPosition:
    direction: str
    entry: float
    sl: float
    tp: float
    entry_at: str
    risk_pts: float


def manage_position(
    pos: CHOCHPosition,
    candle: dict,
    bar_ts: datetime,
) -> tuple[float | None, str]:
    """
    Check SL/TP/square-off on current candle.
    Returns (exit_price, reason) or (None, '').
    """
    high = float(candle["high"])
    low = float(candle["low"])
    close = float(candle["close"])

    if bar_ts.time() >= SQUARE_OFF:
        return close, "SQUARE_OFF_1515"

    if pos.direction == "LONG":
        if low <= pos.sl:
            return pos.sl, "SL_HIT"
        if high >= pos.tp:
            return pos.tp, "TP_HIT"
    else:
        if high >= pos.sl:
            return pos.sl, "SL_HIT"
        if low <= pos.tp:
            return pos.tp, "TP_HIT"
    return None, ""


# ── Per-day backtest ──────────────────────────────────────────────────────────

@dataclass
class DayResult:
    trades: list[dict] = field(default_factory=list)

    def add(self, direction: str, symbol: str, entry: float, exit_price: float,
            reason: str, entry_at: str, exit_at: str, risk_pts: float) -> None:
        pnl = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
        self.trades.append({
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "exit": exit_price,
            "pnl_pts": pnl,
            "risk_pts": risk_pts,
            "rr": round(pnl / risk_pts, 2) if risk_pts else 0,
            "reason": reason,
            "entry_at": entry_at,
            "exit_at": exit_at,
            "result": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE"),
        })


def backtest_day(
    symbol: str,
    candles: list[dict],
    day: date,
) -> DayResult:
    result = DayResult()
    state = StructureState()
    position: CHOCHPosition | None = None
    trades_today = 0

    for i, candle in enumerate(candles):
        ts = parse_candle_ts(candle["timestamp"])
        bar_ts = ts + timedelta(minutes=5)   # bar close time
        closed = candles[: i + 1]

        # Manage open position first
        if position:
            exit_price, reason = manage_position(position, candle, bar_ts)
            if exit_price is not None:
                result.add(
                    position.direction, symbol,
                    position.entry, exit_price,
                    reason, position.entry_at,
                    bar_ts.strftime("%H:%M"),
                    position.risk_pts,
                )
                position = None
            continue

        # Update market structure
        update_structure(state, closed)

        # Skip outside entry window
        t = bar_ts.time()
        if t < ENTRY_START or t > NO_ENTRY_AFTER:
            continue
        if trades_today >= MAX_TRADES_PER_DAY:
            continue

        # CHOCH signal
        direction, choch_level = detect_choch(state, closed)
        if direction is None:
            continue

        # ADX filter
        adx_val = _adx(closed)
        if adx_val is None or adx_val < ADX_MIN:
            continue

        # 1H HTF filter (build 1H from the session candles seen so far)
        htf_direction = get_1h_ema_direction(closed, bar_ts)
        if htf_direction is not None:
            if direction == "LONG" and htf_direction == "BEAR":
                continue
            if direction == "SHORT" and htf_direction == "BULL":
                continue

        # ATR for buffer sizing
        atr_val = _atr(closed)
        if atr_val is None:
            continue

        entry = float(candle["close"])
        buf = atr_val * 0.25   # small buffer beyond structure level

        # SL anchored to the CHOCH structure break level (with buffer)
        if direction == "LONG":
            # choch_level is the SH that was broken → SL just below it
            sl = choch_level - buf
            if sl >= entry:
                continue  # degenerate setup
        else:
            # choch_level is the SL that was broken → SL just above it
            sl = choch_level + buf
            if sl <= entry:
                continue

        sl_dist = abs(entry - sl)
        if sl_dist < 1.0:
            continue   # too tight
        tp_dist = sl_dist * TP_RR

        if direction == "LONG":
            tp = entry + tp_dist
        else:
            tp = entry - tp_dist

        position = CHOCHPosition(
            direction=direction,
            entry=entry,
            sl=sl,
            tp=tp,
            entry_at=bar_ts.strftime("%H:%M"),
            risk_pts=sl_dist,
        )
        trades_today += 1

    # Force flat at end of day
    if position:
        last = candles[-1]
        last_ts = parse_candle_ts(last["timestamp"]) + timedelta(minutes=5)
        result.add(
            position.direction, symbol,
            position.entry, float(last["close"]),
            "EOD", position.entry_at,
            last_ts.strftime("%H:%M"),
            position.risk_pts,
        )

    return result


# ── Full backtest + report ────────────────────────────────────────────────────

def run(days: int = 90) -> None:
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    client = HistoricalDataClient()

    all_trades: list[dict] = []
    skipped = 0

    print(f"\n{'='*62}")
    print(f"  CHOCH Strategy -- {days}-day Backtest ({start_date} -> {end_date})")
    print(f"{'='*62}")

    for code, cfg in INDEX_CONFIGS.items():
        print(f"\n>> {cfg.display} ({code})")
        all_5m = client.fetch_5m(cfg.spot_instrument_key, start_date, end_date)
        if not all_5m:
            print("  No data — skipped")
            skipped += 1
            continue

        trading_day_list = client.trading_days(all_5m, start_date, end_date)
        symbol_trades: list[dict] = []

        for day in trading_day_list:
            day_candles = client.session_5m(all_5m, day)
            if len(day_candles) < 10:
                continue
            result = backtest_day(code, day_candles, day)
            for t in result.trades:
                t["date"] = day.isoformat()
                symbol_trades.append(t)
                all_trades.append({**t, "symbol": code})

        trading_days = len(trading_day_list)

        if not symbol_trades:
            print(f"  0 trades in {trading_days} days")
            continue

        pts = [t["pnl_pts"] for t in symbol_trades]
        wins = sum(1 for p in pts if p > 0)
        losses = sum(1 for p in pts if p < 0)
        total = len(pts)
        wr = 100.0 * wins / total if total else 0
        avg_win = statistics.mean(p for p in pts if p > 0) if wins else 0
        avg_loss = statistics.mean(p for p in pts if p < 0) if losses else 0
        total_pts = sum(pts)
        avg_rr = statistics.mean(t["rr"] for t in symbol_trades)

        print(f"  Trades: {total}  ({trading_days} days)  |  WR: {wr:.1f}%")
        print(f"  P&L: {total_pts:+.1f} pts  |  Avg win: {avg_win:+.1f}  Avg loss: {avg_loss:+.1f}")
        print(f"  Avg R:R achieved: {avg_rr:.2f}  |  Wins: {wins}  Losses: {losses}")

    # ── Combined summary ──────────────────────────────────────────────────────
    if not all_trades:
        print("\n  No trades generated across all indices.")
        return

    print(f"\n{'='*62}")
    print("  COMBINED RESULTS (all indices)")
    print(f"{'='*62}")

    all_pts = [t["pnl_pts"] for t in all_trades]
    wins = sum(1 for p in all_pts if p > 0)
    losses = sum(1 for p in all_pts if p < 0)
    total = len(all_pts)
    total_pts = sum(all_pts)
    wr = 100.0 * wins / total if total else 0
    avg_win = statistics.mean(p for p in all_pts if p > 0) if wins else 0
    avg_loss = statistics.mean(p for p in all_pts if p < 0) if losses else 0
    expectancy = (wr / 100 * avg_win) + ((1 - wr / 100) * avg_loss)

    # Equity curve & drawdown
    equity = CAPITAL
    peak = CAPITAL
    max_dd = 0.0
    daily_pnl: dict[str, float] = {}
    for t in sorted(all_trades, key=lambda x: x["date"]):
        daily_pnl[t["date"]] = daily_pnl.get(t["date"], 0) + t["pnl_pts"]
    lot_value = INDEX_CONFIGS["NIFTY"].lot_size * 50  # NIFTY lot=65, rough INR/pt for equity curve
    for d_pts in daily_pnl.values():
        equity += d_pts * lot_value / 1000   # rough INR approximation
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    daily_pts = list(daily_pnl.values())
    sharpe = (statistics.mean(daily_pts) / statistics.stdev(daily_pts) * (252 ** 0.5)
              if len(daily_pts) > 1 and statistics.stdev(daily_pts) > 0 else 0)
    neg = [p for p in daily_pts if p < 0]
    sortino = (statistics.mean(daily_pts) / statistics.stdev(neg) * (252 ** 0.5)
               if len(neg) > 1 and statistics.stdev(neg) > 0 else 0)
    trading_days_uniq = len(daily_pnl)

    print(f"  Total trades : {total}  over  {trading_days_uniq} trading days")
    print(f"  Win rate     : {wr:.1f}%  ({wins}W / {losses}L)")
    print(f"  Total P&L    : {total_pts:+.1f} pts")
    print(f"  Avg win      : {avg_win:+.1f} pts   |   Avg loss: {avg_loss:+.1f} pts")
    print(f"  Expectancy   : {expectancy:+.2f} pts/trade")
    print(f"  Avg R:R      : {statistics.mean(t['rr'] for t in all_trades):.2f}")
    print(f"  Sharpe       : {sharpe:.2f}   |   Sortino: {sortino:.2f}")
    print(f"  Max drawdown : {max_dd:.1f}%")
    print(f"  Avg trades/day: {total / trading_days_uniq:.1f}")

    # ── INR estimate ─────────────────────────────────────────────────────────
    # Assume ATM option delta 0.5, 1 lot each index
    INR_PER_PT = {
        "NIFTY":    INDEX_CONFIGS["NIFTY"].lot_size * 0.5,
        "BANKNIFTY": INDEX_CONFIGS["BANKNIFTY"].lot_size * 0.5,
        "SENSEX":   INDEX_CONFIGS["SENSEX"].lot_size * 0.5,
    }
    inr_by_index: dict[str, float] = {}
    for t in all_trades:
        sym = t["symbol"]
        rate = INR_PER_PT.get(sym, 32.5)  # fallback: 65 lot × 0.5 delta
        inr_by_index[sym] = inr_by_index.get(sym, 0.0) + t["pnl_pts"] * rate

    total_inr = sum(inr_by_index.values())
    print(f"\n  === INR Estimate (1 lot each, ATM delta~0.5) ===")
    for sym, inr in inr_by_index.items():
        lot_sz = INDEX_CONFIGS[sym].lot_size if sym in INDEX_CONFIGS else 75
        days_sym = trading_days_uniq
        print(f"  {sym:12s} : Rs.{inr:>10,.0f}  ({inr/days_sym:+.0f}/day)")
    print(f"  {'TOTAL':12s} : Rs.{total_inr:>10,.0f}  ({total_inr/trading_days_uniq:+.0f}/day)")
    lots_for_3k = max(1, round(3000 / (total_inr / trading_days_uniq))) if total_inr > 0 else "N/A"
    print(f"\n  Lots needed for Rs.3,000/day target: ~{lots_for_3k} lots/index")

    print(f"\n{'='*62}")
    print("  WARNING: NOT financial advice. Research only.")
    print(f"{'='*62}\n")

    # Daily breakdown
    print("  Daily P&L (pts) - top/bottom 5 days:")
    sorted_days = sorted(daily_pnl.items(), key=lambda x: x[1], reverse=True)
    for d, p in sorted_days[:5]:
        print(f"    {d}  {p:+.1f} pts  WIN")
    print("    ...")
    for d, p in sorted_days[-5:]:
        print(f"    {d}  {p:+.1f} pts  LOSS")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CHOCH Strategy Backtest")
    parser.add_argument("--days", type=int, default=90, help="Lookback days (default: 90)")
    args = parser.parse_args()
    run(args.days)
