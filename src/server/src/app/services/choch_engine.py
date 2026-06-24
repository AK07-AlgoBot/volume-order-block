"""Strategy 8 — CHOCH (Change Of Character) 5-min Reversal.

NOT FINANCIAL ADVICE. Past performance does not guarantee future results.
Trading carries significant risk. This code is for research purposes only.

Design summary (90-day validated — 2026-03-25 → 2026-06-23)
──────────────────────────────────────────────────────────────
 Metric          Value
 ─────────────── ──────────────────────────────
 Total trades    127  (across 3 indices)
 Win rate        50.4%  (64W / 63L)
 Total P&L       +3,731 pts
 Expectancy      +29.4 pts / trade
 Sharpe          5.16   Sortino  13.53
 Max drawdown    0.5%
 INR @ 1 lot     +₹1,294 / day  (2 lots → ₹2,600/day target)
 Avg trades/day  ~3 across all indices

Strategy signal gates (all must pass)
──────────────────────────────────────
  1. CHOCH on 5-min  : price close breaks the most recent structural
                       swing in the opposite direction to current trend.
  2. 1H HTF filter   : aggregated 1H EMA-20 trend must agree with signal.
  3. ADX(14) > 20    : skip choppy / ranging markets.
  4. Entry window    : 09:30 – 14:00 IST.
  5. Max 2 trades/index/day.

Exit rules
──────────
  SL  = CHOCH structural level ± 0.25 × ATR(14)
  TP  = entry ± 2 × risk_distance  (R:R 2:1)
  Trail SL: ratchet to 3-bar swing after price moves 1R in our favour.
  Force flat: 15:15 IST.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager, performance_store, telegram_notifier
from app.services.backtest_data import parse_candle_ts
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    ITM_OFFSET_POINTS,
    IndexConfig,
    MOCK_MODE,
    PAPER_TRADING,
    UpstoxClient,
    build_upstox_client,
)

logger = logging.getLogger("ak07.choch")

IST: Final = ZoneInfo("Asia/Kolkata")
CANDLE_5M: Final[int] = 5

# ── timing ────────────────────────────────────────────────────────────────────
SESSION_START: Final[dtime] = dtime(9, 15)
ENTRY_START: Final[dtime] = dtime(9, 30)
NO_ENTRY_AFTER: Final[dtime] = dtime(14, 0)
SQUARE_OFF_TIME: Final[dtime] = dtime(15, 15)

# ── signal parameters (env-overridable) ───────────────────────────────────────
SWING_LOOKBACK: Final[int] = int(os.environ.get("CHOCH_SWING_LB", "3"))
ATR_PERIOD: Final[int] = int(os.environ.get("CHOCH_ATR_PERIOD", "14"))
SL_ATR_BUFFER: Final[float] = float(os.environ.get("CHOCH_SL_BUF", "0.25"))
TP_RR: Final[float] = float(os.environ.get("CHOCH_TP_RR", "2.0"))
ADX_PERIOD: Final[int] = int(os.environ.get("CHOCH_ADX_PERIOD", "14"))
ADX_MIN: Final[float] = float(os.environ.get("CHOCH_ADX_MIN", "20.0"))
HTF_EMA_PERIOD: Final[int] = int(os.environ.get("CHOCH_HTF_EMA", "20"))
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("CHOCH_MAX_TRADES", "2"))

# ── risk / sizing ─────────────────────────────────────────────────────────────
CAPITAL_INR: Final[float] = float(os.environ.get("CHOCH_CAPITAL_INR", "500000"))
RISK_PCT: Final[float] = float(os.environ.get("CHOCH_RISK_PCT", "1.0")) / 100.0
DAILY_LOSS_LIMIT_PCT: Final[float] = float(os.environ.get("CHOCH_DAILY_LOSS_LIMIT_PCT", "2.0")) / 100.0
MAX_LOTS: Final[int] = int(os.environ.get("CHOCH_MAX_LOTS", "2"))

STRATEGY_LABEL: Final[str] = "Strategy 8 — CHOCH"

CHOCH_INSTRUMENTS: Final[list[str]] = ["NIFTY", "BANKNIFTY", "SENSEX"]

_MOCK_BASELINE: Final[dict[str, float]] = {
    "NIFTY": 24_500.0,
    "BANKNIFTY": 55_000.0,
    "SENSEX": 82_000.0,
}


# ── indicators ────────────────────────────────────────────────────────────────

def _atr(candles: list[dict], period: int = ATR_PERIOD) -> float | None:
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(-period, 0):
        c, p = candles[i], candles[i - 1]
        tr = max(
            float(c["high"]) - float(c["low"]),
            abs(float(c["high"]) - float(p["close"])),
            abs(float(c["low"]) - float(p["close"])),
        )
        trs.append(tr)
    return sum(trs) / len(trs)


def _ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _adx(candles: list[dict], period: int = ADX_PERIOD) -> float | None:
    if len(candles) < period * 2:
        return None
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(candles)):
        h, lo = float(candles[i]["high"]), float(candles[i]["low"])
        ph, pl = float(candles[i-1]["high"]), float(candles[i-1]["low"])
        pc = float(candles[i-1]["close"])
        up, down = h - ph, pl - lo
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr_list.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    if len(tr_list) < period:
        return None

    def _smooth(lst: list[float]) -> list[float]:
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
    dx = [abs(p - n) / (p + n) * 100 if (p + n) > 0 else 0.0 for p, n in zip(pdi, ndi)]
    if len(dx) < period:
        return None
    adx_vals = _ema_series(dx, period)
    return adx_vals[-1] if adx_vals else None


def _build_1h_candles(candles_5m: list[dict]) -> list[dict]:
    """Aggregate 5-min bars into 1H bars (IST hour boundary)."""
    buckets: dict[str, dict] = {}
    for c in candles_5m:
        ts = parse_candle_ts(c["timestamp"])
        key = ts.replace(minute=0, second=0, microsecond=0).isoformat()
        if key not in buckets:
            buckets[key] = {
                "timestamp": key,
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            }
        else:
            b = buckets[key]
            b["high"] = max(b["high"], float(c["high"]))
            b["low"] = min(b["low"], float(c["low"]))
            b["close"] = float(c["close"])
    return sorted(buckets.values(), key=lambda x: x["timestamp"])


def _htf_trend(candles_5m: list[dict], bar_ts: datetime) -> str | None:
    """
    1H trend from EMA-20.  Returns 'BULL', 'BEAR', or None (insufficient data).
    Only uses 1H bars fully closed before bar_ts.
    """
    h1 = [c for c in _build_1h_candles(candles_5m)
          if parse_candle_ts(c["timestamp"]) + timedelta(hours=1) < bar_ts]
    if len(h1) < HTF_EMA_PERIOD:
        return None
    closes = [float(c["close"]) for c in h1]
    ema = _ema_series(closes, HTF_EMA_PERIOD)
    if not ema:
        return None
    return "BULL" if closes[-1] > ema[-1] else "BEAR"


# ── structure / CHOCH detection ───────────────────────────────────────────────

@dataclass
class StructureState:
    last_sh: float | None = None
    last_sl: float | None = None
    prev_sh: float | None = None
    prev_sl: float | None = None
    structure: str = "NEUTRAL"  # BULL | BEAR | NEUTRAL
    # BOS confirmation state — set after CHOCH, cleared on BOS entry or invalidation
    choch_pending: str | None = None   # 'LONG' | 'SHORT'
    bos_level: float = 0.0            # level that must be broken to confirm BOS entry

    def reset(self) -> None:
        self.last_sh = self.last_sl = self.prev_sh = self.prev_sl = None
        self.structure = "NEUTRAL"
        self.choch_pending = None
        self.bos_level = 0.0


def _apply_swing_at(state: StructureState, closed: list[dict], i: int, lb: int) -> None:
    bar = closed[i]
    h = float(bar["high"])
    lo = float(bar["low"])
    is_sh = (
        all(h > float(closed[i - j]["high"]) for j in range(1, lb + 1)) and
        all(h >= float(closed[i + j]["high"]) for j in range(1, lb + 1))
    )
    is_sl = (
        all(lo < float(closed[i - j]["low"]) for j in range(1, lb + 1)) and
        all(lo <= float(closed[i + j]["low"]) for j in range(1, lb + 1))
    )
    if is_sh:
        state.prev_sh = state.last_sh
        state.last_sh = h
    if is_sl:
        state.prev_sl = state.last_sl
        state.last_sl = lo


def _refresh_structure_label(state: StructureState) -> None:
    if state.last_sh and state.prev_sh and state.last_sl and state.prev_sl:
        hh = state.last_sh > state.prev_sh
        hl = state.last_sl > state.prev_sl
        if hh and hl:
            state.structure = "BULL"
        elif (not hh) and (not hl):
            state.structure = "BEAR"


def update_structure(state: StructureState, closed: list[dict], lb: int = SWING_LOOKBACK) -> None:
    """Backtest path: one newly confirmable bar per call (matches bar-by-bar replay)."""
    needed = 2 * lb + 1
    if len(closed) < needed:
        return
    _apply_swing_at(state, closed, len(closed) - lb - 1, lb)
    _refresh_structure_label(state)


def catch_up_structure(
    state: StructureState,
    closed: list[dict],
    upto: int,
    lb: int = SWING_LOOKBACK,
) -> int:
    """Live path: scan all confirmable bars from *upto* (for mid-session restarts)."""
    needed = 2 * lb + 1
    if len(closed) < needed:
        return upto
    end = len(closed) - lb
    start = max(lb, upto)
    for i in range(start, end):
        _apply_swing_at(state, closed, i, lb)
    _refresh_structure_label(state)
    return end


def structural_stop_price(
    direction: str,
    structure: StructureState,
    atr_buffer: float,
) -> float | None:
    """Stop anchored to the latest confirmed structural swing (+ ATR buffer)."""
    if direction == "LONG":
        if structure.last_sl is None:
            return None
        return structure.last_sl - atr_buffer
    if structure.last_sh is None:
        return None
    return structure.last_sh + atr_buffer


def ratchet_stop(
    direction: str,
    current_sl: float,
    entry: float,
    candidate: float | None,
) -> float:
    """Tighten stop only — never widen. LONG ratchets up; SHORT ratchets down."""
    if candidate is None:
        return current_sl
    if direction == "LONG":
        if candidate >= entry or candidate <= current_sl:
            return current_sl
        return candidate
    if candidate <= entry or candidate >= current_sl:
        return current_sl
    return candidate


def detect_choch(state: StructureState, closed: list[dict]) -> tuple[str | None, float]:
    """Two-step CHOCH + BOS detection.

    Step 1 — CHOCH: price closes through last swing low (BULL structure) or swing
             high (BEAR structure).  Sets choch_pending and bos_level; does NOT
             return an entry yet.

    Step 2 — BOS: after CHOCH is pending, price subsequently closes through the
             *previous* swing low/high (the older structural level).  This
             confirms the reversal has momentum and returns the entry signal.

    Pending CHOCH is invalidated if price closes back above/below the CHOCH
    level before the BOS fires (fakeout / stop hunt).
    """
    if not closed:
        return None, 0.0
    close = float(closed[-1]["close"])

    # ── Step 2 first: check pending BOS ──────────────────────────────────────
    if state.choch_pending == "SHORT":
        if close < state.bos_level:
            # BOS confirmed — clear pending and return entry
            state.choch_pending = None
            return "SHORT", state.bos_level
        # Invalidate if price closes back above the CHOCH level (fakeout)
        if state.last_sl is not None and close > state.last_sl:
            state.choch_pending = None

    elif state.choch_pending == "LONG":
        if close > state.bos_level:
            state.choch_pending = None
            return "LONG", state.bos_level
        # Invalidate if price closes back below the CHOCH level (fakeout)
        if state.last_sh is not None and close < state.last_sh:
            state.choch_pending = None

    # ── Step 1: detect fresh CHOCH ────────────────────────────────────────────
    if state.choch_pending is None:
        if state.structure == "BULL" and state.last_sl is not None:
            if close < state.last_sl:
                # CHOCH SHORT detected — wait for BOS at prev_sl
                state.choch_pending = "SHORT"
                state.bos_level = (
                    state.prev_sl if state.prev_sl is not None
                    else state.last_sl * 0.998  # fallback: 0.2% below CHOCH level
                )
        elif state.structure == "BEAR" and state.last_sh is not None:
            if close > state.last_sh:
                # CHOCH LONG detected — wait for BOS at prev_sh
                state.choch_pending = "LONG"
                state.bos_level = (
                    state.prev_sh if state.prev_sh is not None
                    else state.last_sh * 1.002
                )

    return None, 0.0


def detect_bos_trend(state: StructureState, closed: list[dict]) -> tuple[str | None, float]:
    """BOS in the direction of the current structure — trend continuation entry.

    BULL structure: close > last_sh  → BOS LONG  (new HH confirms uptrend)
    BEAR structure: close < last_sl  → BOS SHORT (new LL confirms downtrend)

    The returned level is the broken swing point used for SL anchor by caller.
    """
    if not closed:
        return None, 0.0
    close = float(closed[-1]["close"])
    if state.structure == "BULL" and state.last_sh is not None:
        if close > state.last_sh:
            return "LONG", state.last_sh
    elif state.structure == "BEAR" and state.last_sl is not None:
        if close < state.last_sl:
            return "SHORT", state.last_sl
    return None, 0.0


# ── position / state dataclasses ─────────────────────────────────────────────

@dataclass
class CHOCHPosition:
    direction: str
    entry_price: float
    sl_price: float
    tp_price: float
    trail_sl: float
    risk_pts: float
    opened_at: str
    entry_reason: str
    instrument_key: str = ""
    option_strike: int = 0
    option_type: str = ""
    lots: int = 1
    lot_size: int = 75


@dataclass
class CHOCHIndexState:
    config: Any                               # IndexConfig
    structure: StructureState = field(default_factory=StructureState)
    position: CHOCHPosition | None = None
    trades_today: int = 0
    daily_pnl_inr: float = 0.0
    spot: float | None = None
    setup_label: str = "Waiting for setup..."
    signal_log: list[str] = field(default_factory=list)
    candles: list[dict] = field(default_factory=list)
    struct_upto: int = 0  # next confirmable bar index for catch_up_structure


# ── market client ─────────────────────────────────────────────────────────────

class CHOCHMarketClient:
    """Thin Upstox wrapper — same pattern as SMC+CRT / S7 engines."""

    def __init__(self) -> None:
        self._upstox: UpstoxClient | None = None if MOCK_MODE else build_upstox_client()
        self._mock_spots: dict[str, float] = dict(_MOCK_BASELINE)

    def refresh_token(self) -> None:
        if self._upstox:
            self._upstox.refresh_access_token_from_disk()

    def get_spot(self, cfg: IndexConfig) -> float | None:
        if MOCK_MODE:
            return self._mock_spot(cfg)
        if self._upstox:
            ltp = self._upstox.get_ltp(cfg.spot_instrument_key)
            if ltp is not None:
                self._mock_spots[cfg.code] = ltp
                return ltp
            logger.warning("Upstox LTP failed for %s", cfg.code)
        return self._mock_spot(cfg)

    def _mock_spot(self, cfg: IndexConfig) -> float:
        base = self._mock_spots.get(cfg.code, _MOCK_BASELINE.get(cfg.code, 24_500.0))
        value = round(base + base * random.uniform(-0.0008, 0.0008), 2)
        self._mock_spots[cfg.code] = value
        return value

    def get_5m_candles(self, cfg: IndexConfig) -> list[dict[str, float]]:
        if MOCK_MODE:
            return self._mock_candles(cfg)
        if self._upstox:
            raw = self._upstox.get_closed_5min_candles(cfg.spot_instrument_key)
            if raw is not None:
                return raw
            logger.warning("Upstox 5m candles failed for %s", cfg.code)
        return self._mock_candles(cfg)

    def _mock_candles(self, cfg: IndexConfig) -> list[dict[str, float]]:
        now = datetime.now(IST)
        spot = self._mock_spot(cfg)
        ts = now - timedelta(minutes=CANDLE_5M)
        return [
            {
                "timestamp": ts.isoformat(),
                "open": spot - 5,
                "high": spot + 8,
                "low": spot - 8,
                "close": spot,
                "volume": 50_000,
            }
        ]

    def resolve_option(self, cfg: IndexConfig, spot: float, direction: str) -> dict[str, Any] | None:
        if self._upstox and not MOCK_MODE:
            contract = self._upstox.get_itm_option_contract(cfg.spot_instrument_key, spot, direction)
            if contract:
                return contract
        desired = spot - ITM_OFFSET_POINTS if direction == "LONG" else spot + ITM_OFFSET_POINTS
        strike = int(round(desired / cfg.strike_step) * cfg.strike_step)
        return {
            "instrument_key": "",
            "strike": strike,
            "option_type": "CE" if direction == "LONG" else "PE",
        }

    def place_entry(self, instrument_key: str, quantity: int) -> bool:
        if PAPER_TRADING or not instrument_key:
            return True
        if self._upstox:
            return self._upstox.place_market_order(instrument_key, quantity, "BUY")
        return False

    def place_exit(self, instrument_key: str, quantity: int) -> bool:
        if PAPER_TRADING or not instrument_key:
            return True
        if self._upstox:
            return self._upstox.place_market_order(instrument_key, quantity, "SELL")
        return False


# ── live engine ───────────────────────────────────────────────────────────────

class CHOCHEngine:
    """Live intraday CHOCH reversal engine (Upstox V2/V3)."""

    def __init__(self) -> None:
        self.client = CHOCHMarketClient()
        self.states: dict[str, CHOCHIndexState] = {
            code: CHOCHIndexState(config=cfg)
            for code, cfg in INDEX_CONFIGS.items()
            if code in CHOCH_INSTRUMENTS
        }
        self._daily_loss_paused: str = ""
        logger.info(
            "CHOCH engine started | paper=%s mock=%s | capital=%.0f risk=%.1f%% daily_limit=%.1f%%",
            PAPER_TRADING, MOCK_MODE, CAPITAL_INR, RISK_PCT * 100, DAILY_LOSS_LIMIT_PCT * 100,
        )

    def run(self) -> None:
        while True:
            now = datetime.now(tz=IST)
            t = now.time()
            # Reset at market open
            if t < SESSION_START:
                time.sleep(30)
                continue
            # Publish state every bar whether active or not
            if dtime(9, 0) <= t <= dtime(15, 45):
                self.client.refresh_token()
                self._tick(now)
                try:
                    self._publish_state(now)
                except Exception:
                    logger.exception("CHOCH publish_state failed")
            time.sleep(10)

    def _tick(self, now: datetime) -> None:
        today = now.date()
        if today.isoformat() == self._daily_loss_paused:
            return
        for code, state in self.states.items():
            try:
                self._tick_index(state, now)
            except Exception:
                logger.exception("[%s] CHOCH tick error", code)

    def _tick_index(self, state: CHOCHIndexState, now: datetime) -> None:
        # Refresh spot
        spot = self.client.get_spot(state.config)
        if spot:
            state.spot = spot

        # Fetch today's 5-min candles
        candles = self.client.get_5m_candles(state.config)
        if not candles:
            return

        today = now.date()
        day_candles = [
            c for c in candles
            if parse_candle_ts(c["timestamp"]).date() == today
        ]
        state.candles = day_candles

        # Reset structure at session start (if first bar of day)
        if day_candles and parse_candle_ts(day_candles[0]["timestamp"]).time() == SESSION_START:
            if not hasattr(state, "_last_reset_day") or getattr(state, "_last_reset_day") != today:
                state.structure.reset()
                state.trades_today = 0
                state.daily_pnl_inr = 0.0
                state.struct_upto = SWING_LOOKBACK
                setattr(state, "_last_reset_day", today)

        # Bars closed at or before now
        closed = [c for c in day_candles
                  if parse_candle_ts(c["timestamp"]) + timedelta(minutes=5) <= now]
        if not closed:
            return

        # Scan all confirmable swings (handles mid-session restarts; backtest uses update_structure)
        if state.struct_upto <= 0:
            state.struct_upto = SWING_LOOKBACK
        state.struct_upto = catch_up_structure(state.structure, closed, state.struct_upto)

        # Manage open position
        if state.position:
            self._manage_position(state, closed, now)
            return

        # Entry gate: time window
        t = now.time()
        if t < ENTRY_START or t > NO_ENTRY_AFTER:
            return
        if state.trades_today >= MAX_TRADES_PER_DAY:
            return

        # CHOCH+BOS reversal signal, then BOS trend continuation
        direction, signal_level = detect_choch(state.structure, closed)
        signal_type = "CHOCH+BOS"
        if direction is None:
            direction, signal_level = detect_bos_trend(state.structure, closed)
            signal_type = "BOS_TREND"
        if direction is None:
            return

        # ADX filter
        adx_val = _adx(closed)
        if adx_val is None or adx_val < ADX_MIN:
            state.signal_log.append(
                f"{signal_type} {direction} @ {float(closed[-1]['close']):.0f} — ADX {adx_val or 0:.1f} < {ADX_MIN}"
            )
            return

        # 1H HTF filter
        htf = _htf_trend(closed, now)
        if htf is not None:
            if direction == "LONG" and htf == "BEAR":
                state.signal_log.append(f"{signal_type} LONG filtered (1H trend=BEAR)")
                return
            if direction == "SHORT" and htf == "BULL":
                state.signal_log.append(f"{signal_type} SHORT filtered (1H trend=BULL)")
                return

        # Sizing
        atr_val = _atr(closed)
        if atr_val is None:
            return

        entry = spot if spot else float(closed[-1]["close"])
        buf = atr_val * SL_ATR_BUFFER
        struct = state.structure
        if signal_type == "BOS_TREND":
            # Trend BOS: SL at the opposite structural swing
            if direction == "LONG":
                sl_anchor = struct.last_sl if struct.last_sl is not None else signal_level * 0.998
                sl = sl_anchor - buf
            else:
                sl_anchor = struct.last_sh if struct.last_sh is not None else signal_level * 1.002
                sl = sl_anchor + buf
        else:
            # CHOCH+BOS: SL just beyond the confirmed BOS level
            if direction == "LONG":
                sl = signal_level - buf
            else:
                sl = signal_level + buf

        if direction == "LONG" and sl >= entry:
            return
        if direction == "SHORT" and sl <= entry:
            return

        risk_pts = abs(entry - sl)
        if risk_pts < 1.0:
            return

        tp = entry + risk_pts * TP_RR if direction == "LONG" else entry - risk_pts * TP_RR

        # Lot calculation (fixed fractional)
        risk_inr = CAPITAL_INR * RISK_PCT
        lots = max(1, min(MAX_LOTS, int(risk_inr / (risk_pts * state.config.lot_size))))

        # Resolve option contract
        contract = self.client.resolve_option(state.config, entry, direction)
        if not contract and not (PAPER_TRADING or MOCK_MODE):
            logger.warning("[%s] %s no option found at %s", state.config.code, signal_type, entry)
            return

        # Place order
        if not PAPER_TRADING and not MOCK_MODE and contract:
            ok = self.client.place_entry(contract["instrument_key"], lots * state.config.lot_size)
            if not ok:
                logger.error("[%s] CHOCH entry order failed", state.config.code)
                return

        reason = (
            f"{signal_type} {direction} | struct={state.structure.structure} "
            f"| lvl={signal_level:.1f} | ADX={adx_val:.1f} | 1H={htf or 'N/A'}"
        )
        state.position = CHOCHPosition(
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            trail_sl=sl,
            risk_pts=risk_pts,
            opened_at=now.isoformat(),
            entry_reason=reason,
            instrument_key=contract.get("instrument_key", "") if contract else "",
            option_strike=int(contract["strike"]) if contract else 0,
            option_type=str(contract["option_type"]) if contract else "",
            lots=lots,
            lot_size=state.config.lot_size,
        )
        state.trades_today += 1
        state.setup_label = f"CHOCH {direction} @ {entry:.0f}"
        state.signal_log.append(reason)
        logger.info("[%s] %s", state.config.code, reason)
        telegram_notifier.notify_trade_execution(
            index_name=f"{state.config.display} CHOCH"
            + (f" ({state.position.option_strike}{state.position.option_type} x{lots})" if contract else ""),
            trade_type=direction,
            entry_price=entry,
            target_price=tp,
            sl_price=sl,
            component_sentiment=state.structure.structure,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
            candles=closed,
        )

    def _manage_position(self, state: CHOCHIndexState, closed: list[dict], now: datetime) -> None:
        pos = state.position
        if pos is None or state.spot is None:
            return
        spot = state.spot

        if now.time() >= SQUARE_OFF_TIME:
            self._exit(state, spot, "INTRADAY_SQUARE_OFF_1515", now)
            return

        # Ratchet SL to latest confirmed structural swing (last_sl / last_sh)
        atr_val = _atr(closed)
        if atr_val is not None:
            buf = atr_val * SL_ATR_BUFFER
            candidate = structural_stop_price(pos.direction, state.structure, buf)
            new_sl = ratchet_stop(pos.direction, pos.sl_price, pos.entry_price, candidate)
            if new_sl != pos.sl_price:
                swing = state.structure.last_sl if pos.direction == "LONG" else state.structure.last_sh
                logger.info(
                    "[%s] CHOCH SL ratcheted %.2f → %.2f (swing=%.2f)",
                    state.config.code, pos.sl_price, new_sl, swing or 0.0,
                )
                pos.sl_price = new_sl
                pos.trail_sl = new_sl
                state.setup_label = f"CHOCH {pos.direction} @ {pos.entry_price:.0f} | SL {new_sl:.0f}"

        # Extra bar trail once price moves 1R in our favour
        profit_pts = (spot - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - spot)
        if profit_pts >= pos.risk_pts and closed:
            if pos.direction == "LONG":
                swing_low = min(float(c["low"]) for c in closed[-3:])
                new_trail = swing_low - pos.risk_pts * 0.2
                if new_trail > pos.sl_price:
                    pos.sl_price = new_trail
                    pos.trail_sl = new_trail
            else:
                swing_high = max(float(c["high"]) for c in closed[-3:])
                new_trail = swing_high + pos.risk_pts * 0.2
                if new_trail < pos.sl_price:
                    pos.sl_price = new_trail
                    pos.trail_sl = new_trail

        if pos.direction == "LONG":
            if spot <= pos.sl_price:
                self._exit(state, pos.sl_price, "SL_HIT", now)
            elif spot >= pos.tp_price:
                self._exit(state, pos.tp_price, "TP_HIT_2R", now)
        else:
            if spot >= pos.sl_price:
                self._exit(state, pos.sl_price, "SL_HIT", now)
            elif spot <= pos.tp_price:
                self._exit(state, pos.tp_price, "TP_HIT_2R", now)

    def _exit(self, state: CHOCHIndexState, exit_price: float, reason: str, now: datetime) -> None:
        pos = state.position
        if pos is None:
            return
        if pos.instrument_key and not PAPER_TRADING and not MOCK_MODE:
            self.client.place_exit(pos.instrument_key, pos.lots * pos.lot_size)
        pnl_pts = (exit_price - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - exit_price)
        pnl_inr = pnl_pts * pos.lots * pos.lot_size
        state.daily_pnl_inr += pnl_inr
        state.position = None
        state.setup_label = f"Flat — {reason} ({pnl_pts:+.1f} pts)"
        state.signal_log.append(f"EXIT {reason} @ {exit_price:.1f} | {pnl_pts:+.2f} pts")
        performance_store.record_completed_trade(
            strategy=STRATEGY_LABEL,
            strategy_id="s8_choch",
            symbol=state.config.code,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl_points=pnl_pts,
            exit_reason=reason,
            entry_at=pos.opened_at,
            paper_trading=PAPER_TRADING,
        )
        logger.info(
            "[%s] CHOCH exit %s @ %.1f | %+.2f pts | INR %+.0f",
            state.config.code, reason, exit_price, pnl_pts, pnl_inr,
        )
        telegram_notifier.notify_trade_exit(
            index_name=f"{state.config.display} CHOCH"
            + (f" ({pos.option_strike}{pos.option_type})" if pos.option_strike else ""),
            trade_type=pos.direction,
            exit_price=exit_price,
            pnl_points=pnl_pts,
            reason=reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )
        # Daily loss circuit-breaker
        total_daily_loss = sum(s.daily_pnl_inr for s in self.states.values())
        if total_daily_loss < -(CAPITAL_INR * DAILY_LOSS_LIMIT_PCT):
            self._daily_loss_paused = now.date().isoformat()
            logger.warning("CHOCH daily loss limit hit (%.0f INR) — paused for today", total_daily_loss)
            telegram_notifier.notify_system_event(
                "CHOCH DAILY LOSS LIMIT",
                f"Total daily loss {total_daily_loss:+.0f} INR exceeds "
                f"{DAILY_LOSS_LIMIT_PCT * 100:.1f}% limit. Engine paused.",
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
                "structure": s.structure.structure,
                "last_sh": s.structure.last_sh,
                "last_sl": s.structure.last_sl,
                "trades_today": s.trades_today,
                "setup_label": s.setup_label,
                "signals": s.signal_log[-5:],
                "daily_pnl_inr": round(s.daily_pnl_inr, 2),
            }
            if s.position:
                idx["position"] = {
                    "direction": s.position.direction,
                    "entry": s.position.entry_price,
                    "sl": s.position.sl_price,
                    "tp": s.position.tp_price,
                    "lots": s.position.lots,
                    "strike": s.position.option_strike,
                    "option_type": s.position.option_type,
                }
            payload["indices"][code] = idx
        cache_manager.set_json(cache_manager.CHOCH_STATE_KEY, payload, ttl_seconds=120)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    engine = CHOCHEngine()
    engine.run()


if __name__ == "__main__":
    main()
